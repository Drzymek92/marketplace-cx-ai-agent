"""Module 6 Phase 1 tests — telemetry census + feedback-quality aggregation.

Cost metrics come from telemetry (every turn); quality from feedback (reviewed sample only). Pure
aggregation functions are tested on synthetic rows; one end-to-end test confirms real turns populate
the telemetry store and that aggregation reads it.
"""

from __future__ import annotations

import json

import pytest
import yaml

from scripts.agent.graph import answer, build_graph
from scripts.agent.tools import AgentTools
from scripts.budget import telemetry_store
from scripts.config.loader import Config
from scripts.feedback import store as feedback_store
from scripts.feedback.models import FeedbackRecord
from scripts.improvement import aggregate, datasets, eval as ev, tuning
from scripts.profile import store as profile_store
from tests.test_agent import FakeClient


# --- cost aggregation (telemetry census) -------------------------------------------
def _tel(intent, tokens, llm, tier, cache=False, human=False):
    return {"intent": intent, "est_tokens": tokens, "llm_calls": llm, "respond_tier": tier,
            "cache_hit": cache, "needs_human": human}


def test_aggregate_costs_population():
    rows = [
        _tel("faq", 20, 0, None, cache=True),
        _tel("order_status", 100, 1, "fast"),
        _tel("refund_eligibility", 300, 2, "reason", human=True),
    ]
    c = aggregate.aggregate_costs(rows)
    assert c["turns"] == 3
    assert c["total_tokens"] == 420
    assert c["total_llm_calls"] == 3
    assert c["cache_hit_rate"] == round(1 / 3, 4)
    assert c["escalation_rate"] == round(1 / 3, 4)
    assert c["tier_mix"]["reason"] == 1 and c["tier_mix"]["none"] == 1
    assert c["by_intent"]["refund_eligibility"]["avg_tokens"] == 300.0


def test_aggregate_costs_empty():
    c = aggregate.aggregate_costs([])
    assert c["turns"] == 0 and c["total_tokens"] == 0 and c["cache_hit_rate"] == 0.0


# --- quality aggregation (reviewed sample) -----------------------------------------
def _fb(intent, signal, rating, reviewer):
    return {"intent": intent, "signal_type": signal, "rating": rating, "reviewer": reviewer}


def test_aggregate_quality_sample():
    rows = [
        _fb("refund_eligibility", "rating", 1, "sim"),
        _fb("refund_eligibility", "correction", -1, "sim"),
        _fb("order_status", "rating", 1, "sim-audit"),
    ]
    q = aggregate.aggregate_quality(rows)
    assert q["reviewed"] == 3
    assert q["approval_rate"] == round(2 / 3, 4)
    assert q["correction_rate"] == round(1 / 3, 4)
    assert q["by_reviewer"] == {"sim": 2, "sim-audit": 1}
    assert q["by_intent"]["refund_eligibility"]["correction_rate"] == 0.5


def test_summarize_reports_coverage():
    tel = [_tel("faq", 20, 0, None), _tel("order_status", 100, 1, "fast")]
    fb = [_fb("order_status", "rating", 1, "sim-audit")]
    s = aggregate.summarize(tel, fb)
    assert s["coverage"]["turns"] == 2
    assert s["coverage"]["reviewed"] == 1
    assert s["coverage"]["review_rate"] == 0.5
    assert "biased" in s["coverage"]["note"]
    assert set(s["per_intent"]) == {"faq", "order_status"}
    assert s["per_intent"]["faq"]["quality"] is None  # faq never reviewed → cost only


# --- end-to-end: real turns populate the telemetry census --------------------------
@pytest.fixture()
def tools():
    return AgentTools(client=FakeClient())


@pytest.fixture()
def mock_llm(monkeypatch):
    def setup(intent, order_id=None, confidence=0.9):
        monkeypatch.setattr("scripts.llm_client.llm_json",
                            lambda *a, **k: {"intent": intent, "order_id": order_id, "confidence": confidence})
        monkeypatch.setattr("scripts.llm_client.llm_call", lambda *a, **k: f"[answer for {intent}]")
    return setup


def test_turns_populate_telemetry_census(fresh_profile_db, tools, mock_llm, monkeypatch):
    monkeypatch.setattr("scripts.feedback.capture._should_audit", lambda rate: False)  # isolate: no audit noise
    # one routine turn + one flagged (high-stakes) turn
    answer("Can I return ORD-RET?", buyer_id="BUY-1001", tools=tools)
    answer("Refund on ORD-OLD?", buyer_id="BUY-1001", tools=tools)

    rows = telemetry_store.all_records()
    assert telemetry_store.count() == 2          # EVERY turn recorded, flagged or not
    assert feedback_store.count() == 1           # only the flagged turn was reviewed

    summary = aggregate.from_stores()
    assert summary["cost"]["turns"] == 2         # cost census = both turns
    assert summary["quality"]["reviewed"] == 1   # quality sample = one
    assert summary["coverage"]["review_rate"] == 0.5
    intents = {r["intent"] for r in rows}
    assert {"return_check", "refund_eligibility"} <= intents


# --- Phase 2: eval harness ---------------------------------------------------------
def test_score_case_axes():
    state = {
        "intent": "refund_eligibility", "needs_human": True,
        "retrieved": {"check_eligibility": {"eligible": False, "rule_version": "v1"}},
        "meta": {"telemetry": {"est_tokens": 140}},
    }
    case = {"id": "c1", "expected_intent": "refund_eligibility", "expected_needs_human": True,
            "expected_rule": {"eligible": False}}
    r = ev.score_case(state, case)
    assert r["intent_correct"] and r["needs_human_correct"] and r["rule_correct"]
    assert r["tokens"] == 140


def test_aggregate_eval_and_failures():
    per_case = [
        {"id": "a", "intent_correct": True, "rule_correct": True, "tokens": 100},
        {"id": "b", "intent_correct": False, "needs_human_correct": True, "tokens": 50},
    ]
    agg = ev.aggregate_eval(per_case)
    assert agg["n"] == 2
    assert agg["intent_accuracy"] == 0.5
    assert agg["overall_accuracy"] == round(3 / 4, 4)  # 3 of 4 scored flags true
    assert agg["avg_tokens"] == 75.0
    assert agg["failures"] == ["b"]


def test_compare_gate():
    base = {"overall_accuracy": 0.80, "avg_tokens": 100}
    better = {"overall_accuracy": 0.85, "avg_tokens": 90}
    marginal = {"overall_accuracy": 0.805, "avg_tokens": 90}
    pricey = {"overall_accuracy": 0.90, "avg_tokens": 500}
    assert ev.compare(base, better, min_delta=0.02)["promote"] is True
    assert ev.compare(base, marginal, min_delta=0.02)["promote"] is False         # delta too small
    assert ev.compare(base, pricey, min_delta=0.02, max_avg_tokens=200)["promote"] is False  # over budget
    assert ev.compare(base, pricey, min_delta=0.02, max_avg_tokens=200)["beats_baseline"] is True


def test_committed_baseline_parses():
    cases = ev.load_baseline()
    assert len(cases) >= 6
    assert all("message" in c and "expected_intent" in c for c in cases)
    assert {"commission_query", "faq"} <= {c["expected_intent"] for c in cases}


def test_run_eval_scores_agent(fresh_profile_db, tools, mock_llm, monkeypatch):
    monkeypatch.setattr("scripts.feedback.capture._should_audit", lambda rate: False)
    mock_llm("ignored")  # all cases are keyword-trusted → no classify LLM; respond is mocked
    cases = [
        {"id": "ret", "message": "Can I return ORD-RET?", "buyer_id": "BUY-1001",
         "expected_intent": "return_check", "expected_needs_human": False,
         "expected_rule": {"returnable": True}},
        {"id": "refund", "message": "Refund on ORD-OLD?", "buyer_id": "BUY-1001",
         "expected_intent": "refund_eligibility", "expected_needs_human": True,
         "expected_rule": {"eligible": False}},
    ]
    agg = ev.run_eval(cases, tools=tools)
    assert agg["n"] == 2
    assert agg["intent_accuracy"] == 1.0
    assert agg["needs_human_accuracy"] == 1.0
    assert agg["rule_accuracy"] == 1.0
    assert agg["overall_accuracy"] == 1.0
    assert agg["failures"] == []


# --- Phase 3: Track A tuning + eval-gated promotion --------------------------------
def test_apply_patch_deep_merges():
    cfg = tuning.apply_patch(Config(), {"classify": {"fewshot": [{"message": "m", "intent": "faq"}]}})
    assert cfg.classify.fewshot == [{"message": "m", "intent": "faq"}]
    assert cfg.modules.feedback_capture is True   # untouched keys preserved


def test_generate_fewshot_candidate_from_feedback(fresh_profile_db):
    # an approved turn in history + the matching feedback row
    tid = profile_store.append_turn("BUY-1001", "is ORD-4001 returnable?", "return_check", "yes")
    feedback_store.record(FeedbackRecord(
        intent="return_check", signal_type="rating", reviewer="sim", turn_id=tid, rating=1))
    cand = tuning.generate_fewshot_candidate(feedback_store.all_records())
    assert cand and cand["label"] == "fewshot-from-feedback"
    assert {"message": "is ORD-4001 returnable?", "intent": "return_check"} in cand["patch"]["classify"]["fewshot"]


def test_generate_fewshot_candidate_none_without_approvals():
    rows = [{"intent": "refund_eligibility", "signal_type": "correction", "rating": -1, "turn_id": 9}]
    assert tuning.generate_fewshot_candidate(rows) is None


def test_classify_node_uses_config_fewshot(fresh_profile_db, monkeypatch):
    seen = {}
    monkeypatch.setattr("scripts.llm_client.llm_json",
                        lambda prompt, system=None, model=None, **k: seen.setdefault("system", system)
                        or {"intent": "faq", "order_id": None, "confidence": 0.5})
    monkeypatch.setattr("scripts.llm_client.llm_call", lambda *a, **k: "ok")
    cfg = Config()
    cfg.classify.fewshot = [{"message": "do you ship to germany", "intent": "faq"}]
    graph = build_graph(AgentTools(client=FakeClient()), cfg)
    graph.invoke({"message": "hey can you help with something weird", "buyer_id": None})  # ambiguous → LLM classify
    assert "do you ship to germany" in seen["system"]   # exemplar reached the classifier prompt


def test_run_cycle_promotes_better_candidate(tmp_path):
    cand = {"label": "fewshot-from-feedback", "patch": {"classify": {"fewshot": [{"message": "m", "intent": "faq"}]}}}

    def fake_eval(cfg):  # patched cfg (has fewshot) scores better than baseline
        return {"overall_accuracy": 0.90 if cfg.classify.fewshot else 0.80, "avg_tokens": 100}

    out = tuning.run_cycle(candidates=[cand], eval_cfg_fn=fake_eval, min_delta=0.02,
                           write=True, config_path=str(tmp_path / "config.yaml"))
    assert out["promoted"] == "fewshot-from-feedback"
    assert out["candidates"][0]["decision"]["promote"] is True
    written = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert written["classify"]["fewshot"] == [{"message": "m", "intent": "faq"}]


def test_run_cycle_rejects_when_no_gain(tmp_path):
    cand = {"label": "fewshot-from-feedback", "patch": {"classify": {"fewshot": [{"message": "m", "intent": "faq"}]}}}

    def fake_eval(cfg):  # no improvement
        return {"overall_accuracy": 0.80, "avg_tokens": 100}

    out = tuning.run_cycle(candidates=[cand], eval_cfg_fn=fake_eval, min_delta=0.02,
                           write=True, config_path=str(tmp_path / "config.yaml"))
    assert out["promoted"] is None
    assert not (tmp_path / "config.yaml").exists()   # nothing written on reject


def test_write_promotion_preserves_other_sections(tmp_path):
    import yaml as _yaml
    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text(_yaml.safe_dump({"rules": {"version": "v1"}, "privacy": {"mask_pii_in_logs": True}}))
    tuning.write_promotion({"classify": {"fewshot": [{"message": "m", "intent": "faq"}]}}, str(cfgfile))
    out = _yaml.safe_load(cfgfile.read_text())
    assert out["classify"]["fewshot"]                      # added
    assert out["privacy"]["mask_pii_in_logs"] is True      # preserved
    assert out["rules"]["version"] == "v1"                 # preserved


# --- Phase 4: Track B dataset export -----------------------------------------------
def test_build_dpo_pairs_from_corrections(fresh_profile_db):
    tid = profile_store.append_turn("BUY-1002", "I want a refund on ORD-4002", "refund_eligibility",
                                    "A human will review this.")
    feedback_store.record(FeedbackRecord(intent="refund_eligibility", signal_type="correction",
                                         reviewer="sim", turn_id=tid, rating=-1,
                                         correction="Ask the buyer for the order number first."))
    pairs = datasets.build_dpo_pairs(feedback_store.all_records())
    assert pairs == [{"prompt": "I want a refund on ORD-4002",
                      "chosen": "Ask the buyer for the order number first.",
                      "rejected": "A human will review this.",
                      "intent": "refund_eligibility"}]


def test_build_sft_pairs_from_approvals(fresh_profile_db):
    tid = profile_store.append_turn("BUY-1003", "what is your return policy?", "faq",
                                    "You can return most items within 14 days.")
    feedback_store.record(FeedbackRecord(intent="faq", signal_type="rating", reviewer="sim",
                                         turn_id=tid, rating=1))
    sft = datasets.build_sft_pairs(feedback_store.all_records())
    assert sft == [{"prompt": "what is your return policy?",
                    "completion": "You can return most items within 14 days.", "intent": "faq"}]


def test_export_datasets_writes_jsonl(fresh_profile_db, tmp_path):
    t1 = profile_store.append_turn("BUY-1002", "refund on ORD-4002", "refund_eligibility", "review banner")
    t2 = profile_store.append_turn("BUY-1003", "return policy?", "faq", "14 days, unused.")
    feedback_store.record(FeedbackRecord(intent="refund_eligibility", signal_type="correction",
                                         reviewer="sim", turn_id=t1, rating=-1, correction="Ask for ORD-####."))
    feedback_store.record(FeedbackRecord(intent="faq", signal_type="rating", reviewer="sim",
                                         turn_id=t2, rating=1))
    out = datasets.export_datasets(out_dir=str(tmp_path), timestamp="20260618_120000")
    assert out["counts"] == {"dpo": 1, "sft": 1}
    dpo_lines = (tmp_path / "dpo_pairs_20260618_120000.jsonl").read_text().strip().splitlines()
    sft_lines = (tmp_path / "sft_dataset_20260618_120000.jsonl").read_text().strip().splitlines()
    assert len(dpo_lines) == 1 and len(sft_lines) == 1
    assert json.loads(dpo_lines[0])["chosen"] == "Ask for ORD-####."
    assert json.loads(sft_lines[0])["completion"] == "14 days, unused."


def test_export_datasets_empty(tmp_path):
    out = datasets.export_datasets(out_dir=str(tmp_path), feedback_rows=[], timestamp="20260618_120000")
    assert out["counts"] == {"dpo": 0, "sft": 0}
    assert (tmp_path / "dpo_pairs_20260618_120000.jsonl").read_text() == ""
