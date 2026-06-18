"""LangGraph agent core (Module 3) + cascade/telemetry (Module 7 slice).

Flow:  triage -> [faq short-circuit | LLM classify | trusted triage] -> retrieve
              -> stakes_gate -> respond -> finalize

Cost discipline (the token win):
  - triage is a zero-LLM deterministic classifier. If it's confident, the LLM classify call
    is SKIPPED entirely. If the message is an order-independent policy FAQ, a canned answer is
    returned with ZERO LLM calls (no classify, no respond).
  - only ambiguous messages pay for the LLM classifier; only the respond step composes prose.
Everything financial is still decided by the versioned rules engine via the tool registry.

Tier routing + escalation policy live in scripts/budget/router.py (Module 7); per-turn cost
telemetry in scripts/budget/telemetry.py.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Callable, Optional

from langgraph.graph import END, START, StateGraph

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

from scripts.agent import faq
from scripts.agent.state import AgentState
from scripts.agent.tools import AgentTools
from scripts.agent.triage import deterministic_classify, find_order_id
from scripts.budget import cache, governor, telemetry, telemetry_store
from dataclasses import asdict
from scripts.config.loader import Config, load_config
from scripts.feedback import capture as feedback_capture
from scripts.profile import context as profile_ctx
from scripts.budget.router import (
    CLASSIFY_TIER,
    CONFIDENCE_FLOOR,
    HIGH_STAKES,
    TRIAGE_CONFIDENCE_FLOOR,
    cap_tier,
    model_for,
    respond_tier,
)
from scripts.logger import get_logger

logger = get_logger("agent")

# Canonical intents -> (tool, process). process is for the eligibility tool.
INTENT_TOOL = {
    "order_status": ("fetch_order", None),
    "return_check": ("check_return", None),
    "refund_eligibility": ("check_eligibility", "refund"),
    "dispute_eligibility": ("check_eligibility", "dispute"),
    "buyer_protection": ("check_eligibility", "buyer_protection"),
    "commission_query": ("compute_order_commission", None),
    "faq": (None, None),
    "unknown": (None, None),
    # NB: there is intentionally no intent mapped to the `submit_return` write mutation — the agent
    # never opens a return autonomously off an LLM classification. See AgentTools.submit_return.
}

CLASSIFY_SYSTEM = (
    "You classify a marketplace customer-support message for an Allegro-style store. "
    "Return ONLY JSON with keys: intent, order_id, confidence. "
    "intent is one of: order_status, return_check, refund_eligibility, dispute_eligibility, "
    "buyer_protection, commission_query, faq, unknown. "
    "Use 'return_check' for 'can I return this / is it returnable'. "
    "Use 'refund_eligibility' only when the user asks about getting money back / a refund. "
    "Use 'commission_query' for seller fee/commission questions. "
    "Use 'faq' for general policy questions with no specific order. "
    "order_id is the ORD-#### code if present, else null. "
    "confidence is your certainty 0.0-1.0. Output JSON only, no prose."
)


# --- governor: meter spend vs ceilings, decide degradation (Module 7) ---------------
def make_governor_node(cfg: Config) -> Callable[[AgentState], dict]:
    def governor_node(state: AgentState) -> dict:
        directive = governor.decide(state.get("buyer_id"), cfg)
        if directive.level:
            logger.info(f"governor: {directive.reason}")
        return {"governor": asdict(directive)}
    return governor_node


# --- load context: profile + history, already budget-trimmed (Module 4) ------------
def make_load_context_node(cfg: Config) -> Callable[[AgentState], dict]:
    def load_context_node(state: AgentState) -> dict:
        if (state.get("governor") or {}).get("shrink_context"):
            logger.info("context: shrunk to zero by governor (budget degradation)")
            return {"context": {"profile": None, "history_turns": [], "history_summary": None}}
        ctx = profile_ctx.build_context(state.get("buyer_id"), cfg)
        if ctx.get("profile") or ctx.get("history_turns") or ctx.get("history_summary"):
            logger.info(
                f"context: profile={'y' if ctx.get('profile') else 'n'} "
                f"turns={len(ctx.get('history_turns') or [])} "
                f"summary={'y' if ctx.get('history_summary') else 'n'}"
            )
        return {"context": ctx}
    return load_context_node


def _context_block(state: AgentState) -> str:
    return profile_ctx.format_for_prompt(state.get("context") or {})


# --- triage: cheap deterministic first rung ----------------------------------------
def triage_node(state: AgentState) -> dict:
    message = state["message"]

    canned = faq.lookup(message)
    if canned is not None:
        logger.info("triage: FAQ short-circuit (0 LLM calls)")
        return {
            "intent": "faq",
            "order_id": None,
            "process": None,
            "confidence": 0.95,
            "answer": canned,
            "needs_human": False,
            "retrieved": {},
            "tool_calls": [],
            "meta": {"classify_mode": "faq", "respond_tier": None, "responded_with_llm": False},
        }

    det = deterministic_classify(message)
    if det["confidence"] >= TRIAGE_CONFIDENCE_FLOOR:
        _, process = INTENT_TOOL.get(det["intent"], (None, None))
        logger.info(f"triage: trusted (intent={det['intent']} conf={det['confidence']:.2f}) — skipping LLM classify")
        return {
            "intent": det["intent"],
            "order_id": det["order_id"],
            "process": process,
            "confidence": det["confidence"],
            "meta": {"classify_mode": "triage", "classify_tier": None},
        }

    logger.info(f"triage: ambiguous (conf={det['confidence']:.2f}) — escalating to LLM classify")
    return {"meta": {"classify_mode": "pending"}}


def _triage_route(state: AgentState) -> str:
    mode = (state.get("meta") or {}).get("classify_mode")
    if mode == "faq":
        return "answered"
    if mode == "triage":
        return "retrieve"
    return "classify"


# --- LLM classify (only reached when triage was unsure) ----------------------------
def _fewshot_block(cfg: Optional[Config]) -> str:
    pairs = [(e.get("message"), e.get("intent")) for e in (cfg.classify.fewshot if cfg else []) or []
             if e.get("message") and e.get("intent")]
    if not pairs:
        return ""
    return "\nExamples:\n" + "\n".join(f"- {m!r} -> {i}" for m, i in pairs)


def make_classify_node(cfg: Optional[Config]) -> Callable[[AgentState], dict]:
    # Few-shot exemplars are config-driven so Module 6 Track A can tune them from feedback.
    system = CLASSIFY_SYSTEM + _fewshot_block(cfg)

    def classify_node(state: AgentState) -> dict:
        message = state["message"]
        try:
            from scripts.llm_client import llm_json

            ctx_block = _context_block(state)
            prompt = f"{ctx_block}\n\nMESSAGE:\n{message}" if ctx_block else message
            raw = llm_json(prompt, system=system, model=model_for(CLASSIFY_TIER))
            intent = str(raw.get("intent", "unknown"))
            if intent not in INTENT_TOOL:
                intent = "unknown"
            order_id = raw.get("order_id") or None
            confidence = float(raw.get("confidence", 0.5))
        except Exception as exc:
            logger.warning(f"classify LLM failed ({exc}); falling back to deterministic triage")
            det = deterministic_classify(message)
            intent, order_id, confidence = det["intent"], det["order_id"], det["confidence"]

        found = find_order_id(message)  # literal ORD-#### always wins
        if found:
            order_id = found

        _, process = INTENT_TOOL.get(intent, (None, None))
        logger.info(f"classify(llm): intent={intent} order_id={order_id} conf={confidence:.2f}")
        return {
            "intent": intent,
            "order_id": order_id,
            "process": process,
            "confidence": confidence,
            "meta": {"classify_mode": "llm", "classify_tier": CLASSIFY_TIER},
        }

    return classify_node


# --- retrieve ----------------------------------------------------------------------
def make_retrieve_node(tools: AgentTools) -> Callable[[AgentState], dict]:
    def retrieve_node(state: AgentState) -> dict:
        return _retrieve(state, tools)
    return retrieve_node


def _retrieve(state: AgentState, tools: AgentTools) -> dict:
    intent = state.get("intent", "unknown")
    order_id = state.get("order_id")
    buyer_id = state.get("buyer_id")
    tool_name, process = INTENT_TOOL.get(intent, (None, None))

    retrieved: dict = {}
    calls: list[str] = []

    if tool_name and order_id:
        kwargs = {"order_id": order_id}
        if process:
            kwargs["process"] = process
        result = tools.call(tool_name, **kwargs)
        calls.append(tool_name)
        retrieved[tool_name] = result.data if result.ok else {"error": result.error}
    elif tool_name and not order_id and buyer_id and intent == "order_status":
        result = tools.call("list_buyer_orders", buyer_id=buyer_id, first=5)
        calls.append("list_buyer_orders")
        retrieved["list_buyer_orders"] = result.data if result.ok else {"error": result.error}
    elif tool_name and not order_id:
        retrieved["_missing"] = "no order_id supplied"

    return {"retrieved": retrieved, "tool_calls": calls}


# --- stakes gate -------------------------------------------------------------------
def stakes_gate_node(state: AgentState) -> dict:
    intent = state.get("intent", "unknown")
    confidence = state.get("confidence", 1.0)
    reasons = []
    if intent in HIGH_STAKES:
        reasons.append(f"high-stakes intent '{intent}'")
    if confidence < CONFIDENCE_FLOOR:
        reasons.append(f"low confidence {confidence:.2f} < {CONFIDENCE_FLOOR}")
    if (state.get("governor") or {}).get("force_human"):
        reasons.append("budget ceiling — handoff to human")
    needs_human = bool(reasons)
    if needs_human:
        logger.info(f"HITL flag: {'; '.join(reasons)}")
    return {"needs_human": needs_human, "hitl_reason": "; ".join(reasons) or None}


# --- respond -----------------------------------------------------------------------
RESPOND_SYSTEM = (
    "You are a customer-support assistant for an Allegro-style marketplace. "
    "Answer the user using ONLY the FACTS provided — never invent order details, dates, "
    "amounts, or eligibility outcomes. If a fact is missing (e.g. no order id), ask for it. "
    "When the FACTS include a decision with a rule_version, base your answer on that decision "
    "and briefly state the reason. Be concise and friendly. "
    "If a USER PROFILE is given, honour its locale and tone_pref and take recent_issues into "
    "account; if RECENT TURNS or an EARLIER CONVERSATION summary are given, stay consistent with "
    "them and don't re-ask what was already answered. "
    "Reply in the same language the user used (Polish or English)."
)


def _maybe_banner(state: AgentState, answer: str) -> str:
    if state.get("needs_human"):
        return ("⚠️ This involves a sensitive request, so a human agent will review it before it's "
                f"final.\n\n{answer}")
    return answer


def make_respond_node(cfg: Config) -> Callable[[AgentState], dict]:
    cache_on = cfg.modules.semantic_cache
    rule_ver = cfg.rules.version

    def respond_node(state: AgentState) -> dict:
        gov = state.get("governor") or {}
        tier = cap_tier(respond_tier(state.get("intent"), state.get("needs_human", False)), gov.get("cap_tier"))
        ctx_block = _context_block(state)
        # cache only order-independent, NON-personalized answers (no order id, no profile/history block)
        cache_eligible = cache_on and not ctx_block and cache.is_eligible(state["message"], state.get("order_id"))

        # Budget degradation: cached-or-canned — try the cache, else a templated answer; no LLM.
        if gov.get("canned_only"):
            cached = cache.lookup(state["message"], rule_version=rule_ver) if cache_eligible else None
            logger.info(f"respond: canned-only (budget) — {'cache hit' if cached is not None else 'templated'}, no LLM")
            meta = dict(state.get("meta", {}))
            meta.update({
                "respond_tier": None, "respond_model": None, "responded_with_llm": False,
                "tools_used": state.get("tool_calls", []), "needs_human": state.get("needs_human", False),
                "rule_version": _rule_version(state), "degraded": "canned_only",
                "cache": "semantic" if cached is not None else None,
            })
            answer = cached if cached is not None else _templated_answer(state)
            return {"answer": _maybe_banner(state, answer), "meta": meta}

        # Semantic cache: a repeat (or paraphrased) order-independent question skips the respond LLM.
        if cache_eligible:
            cached = cache.lookup(state["message"], rule_version=rule_ver)
            if cached is not None:
                meta = dict(state.get("meta", {}))
                meta.update({
                    "respond_tier": None, "respond_model": None, "responded_with_llm": False,
                    "tools_used": state.get("tool_calls", []), "needs_human": state.get("needs_human", False),
                    "rule_version": _rule_version(state), "cache": "semantic",
                })
                return {"answer": _maybe_banner(state, cached), "meta": meta}

        facts = {
            "intent": state.get("intent"),
            "order_id": state.get("order_id"),
            "retrieved": state.get("retrieved", {}),
        }
        prompt = (
            (f"{ctx_block}\n\n" if ctx_block else "")
            + f"User message:\n{state['message']}\n\n"
            + f"FACTS (authoritative — use only these):\n{json.dumps(facts, ensure_ascii=False, indent=2)}"
        )
        responded_with_llm = True
        try:
            from scripts.llm_client import llm_call

            answer = llm_call(prompt, system=RESPOND_SYSTEM, model=model_for(tier)).strip()
        except Exception as exc:
            logger.warning(f"respond LLM failed ({exc}); returning a templated answer")
            answer = _templated_answer(state)
            responded_with_llm = False

        # Cache a fresh, eligible, LLM-produced answer so the next paraphrase is free.
        if cache_eligible and responded_with_llm:
            cache.store(state["message"], answer, rule_version=rule_ver)

        meta = dict(state.get("meta", {}))
        meta.update({
            "respond_tier": tier,
            "respond_model": model_for(tier),
            "responded_with_llm": responded_with_llm,
            "tools_used": state.get("tool_calls", []),
            "needs_human": state.get("needs_human", False),
            "rule_version": _rule_version(state),
        })
        return {"answer": _maybe_banner(state, answer), "meta": meta}

    return respond_node


# --- finalize: assemble telemetry + persist the turn to history ---------------------
def make_finalize_node(cfg: Config) -> Callable[[AgentState], dict]:
    def finalize_node(state: AgentState) -> dict:
        meta = dict(state.get("meta", {}))
        record = telemetry.build_record({**state, "meta": meta})
        meta["telemetry"] = record
        logger.info(
            f"turn: intent={record['intent']} llm_calls={record['llm_calls']} "
            f"mode={record['classify_mode']} cache_hit={record['cache_hit']} "
            f"respond_tier={record['respond_tier']} ~tokens={record['est_tokens']}"
        )
        # record every path (FAQ included) so history + rolling summary stay complete
        turn_id = profile_ctx.record_turn(
            state.get("buyer_id"), state.get("message", ""),
            state.get("intent", "unknown"), state.get("answer", ""), cfg,
        )
        meta["turn_id"] = turn_id
        # Persist telemetry for EVERY turn (Module 7/9) — the unbiased cost/volume census that
        # Module 6 aggregates. Best-effort: observability must never break serving.
        try:
            telemetry_store.record(record, turn_id=turn_id, buyer_id=state.get("buyer_id"))
        except Exception as exc:
            logger.warning(f"telemetry persist failed ({exc})")
        # HITL feedback (Module 5): a simulated reviewer signs off flagged turns. Best-effort —
        # a feedback failure must never break the turn.
        try:
            fb_id = feedback_capture.capture_turn({**state, "meta": meta}, cfg)
            if fb_id is not None:
                meta["feedback_id"] = fb_id
        except Exception as exc:
            logger.warning(f"feedback capture failed ({exc})")
        return {"meta": meta}
    return finalize_node


def _rule_version(state: AgentState) -> Optional[str]:
    for data in (state.get("retrieved") or {}).values():
        if isinstance(data, dict) and data.get("rule_version"):
            return data["rule_version"]
    return None


def _templated_answer(state: AgentState) -> str:
    retrieved = state.get("retrieved", {})
    for data in retrieved.values():
        if isinstance(data, dict) and data.get("reasons"):
            return "Decision: " + "; ".join(data["reasons"])
    if "_missing" in retrieved:
        return "Could you share the order number (ORD-####) so I can look into it?"
    return "I'm not able to answer that right now — please try rephrasing or contact support."


def build_graph(tools: Optional[AgentTools] = None, cfg: Optional[Config] = None) -> "CompiledStateGraph":
    tools = tools or AgentTools()
    cfg = cfg or load_config()
    g = StateGraph(AgentState)
    g.add_node("governor", make_governor_node(cfg))
    g.add_node("load_context", make_load_context_node(cfg))
    g.add_node("triage", triage_node)
    g.add_node("classify", make_classify_node(cfg))
    g.add_node("retrieve", make_retrieve_node(tools))
    g.add_node("stakes_gate", stakes_gate_node)
    g.add_node("respond", make_respond_node(cfg))
    g.add_node("finalize", make_finalize_node(cfg))

    g.add_edge(START, "governor")
    g.add_edge("governor", "load_context")
    g.add_edge("load_context", "triage")
    g.add_conditional_edges("triage", _triage_route,
                            {"answered": "finalize", "retrieve": "retrieve", "classify": "classify"})
    g.add_edge("classify", "retrieve")
    g.add_edge("retrieve", "stakes_gate")
    g.add_edge("stakes_gate", "respond")
    g.add_edge("respond", "finalize")
    g.add_edge("finalize", END)
    return g.compile()


_GRAPH = None


def answer(message: str, buyer_id: Optional[str] = None, tools: Optional[AgentTools] = None) -> AgentState:
    """Run one turn through the agent. Returns the full final state (answer + meta + telemetry).

    Pass `tools` to inject a shared/mocked AgentTools (used by tests and by the server, which
    reuses one client). Without it a default graph is built and cached.
    """
    global _GRAPH
    if tools is not None:
        graph = build_graph(tools)
    else:
        if _GRAPH is None:
            _GRAPH = build_graph()
        graph = _GRAPH
    return graph.invoke({"message": message, "buyer_id": buyer_id})
