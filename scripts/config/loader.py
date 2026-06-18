"""Typed config loader (pydantic). Reads config/config.yaml, falling back to
config/config.example.yaml. `rules`, `budget.context`, and `modules` are modeled;
the remaining sections are tolerated (extra="ignore") and modeled as their modules land.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field


class ReturnsRules(BaseModel):
    window_days: int = 14
    smart_free_return: bool = True
    non_returnable_categories: list[str] = Field(default_factory=list)


class CommissionRules(BaseModel):
    default_pct: float = 0.10
    per_category_pct: dict[str, float] = Field(default_factory=dict)
    transaction_fee_pln: float = 0.0


class EligibilityRules(BaseModel):
    dispute_window_days: int = 30


class RulesConfig(BaseModel):
    version: str = "v1"
    returns: ReturnsRules = Field(default_factory=ReturnsRules)
    commission: CommissionRules = Field(default_factory=CommissionRules)
    eligibility: EligibilityRules = Field(default_factory=EligibilityRules)


class ContextBudget(BaseModel):
    """How much profile/history context we are willing to pay for (Module 4 ∩ governor)."""
    max_history_turns: int = 8
    summarize_history_above_turns: int = 8
    profile_fields: list[str] = Field(
        default_factory=lambda: ["smart_status", "locale", "tone_pref", "recent_issues"]
    )
    max_graphql_records: int = 20


class BudgetConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    context: ContextBudget = Field(default_factory=ContextBudget)
    # Ceilings (0 = unlimited). The governor (Module 7) meters spend against these and degrades.
    per_turn_tokens: int = 0
    per_conversation_tokens: int = 0
    daily_tokens: int = 0
    currency_cap_usd_per_day: float = 0.0
    usd_per_1k_tokens: float = 0.0      # nominal rate for the optional USD cap (0 = USD cap disabled)
    degradation_when_over_budget: list[str] = Field(default_factory=lambda: [
        "drop_to_cheaper_tier", "shrink_context", "cached_or_canned_only", "handoff_to_human"])


class FeedbackConfig(BaseModel):
    """HITL feedback capture (Module 5)."""
    model_config = ConfigDict(extra="ignore")
    channels: list[str] = Field(
        default_factory=lambda: ["thumbs", "free_text_correction", "agent_edit", "escalation_flag"]
    )
    store: str = "sqlite"
    simulate_reviewer: bool = True       # the chosen HITL realism: a simulated stand-in reviewer
    audit_sample_rate: float = 0.05      # unbiased random-sample audit of NON-flagged turns (COPC/A2I)


class ModulesConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_profile: bool = True
    conversation_history: bool = True
    feedback_capture: bool = True        # Module 5: collect ratings/corrections for the improvement loop
    semantic_cache: bool = True          # Module 7: cache order-independent answers (skip respond LLM)


class ClassifyConfig(BaseModel):
    """LLM-classifier tuning surface (Module 6 Track A writes here)."""
    model_config = ConfigDict(extra="ignore")
    fewshot: list[dict] = Field(default_factory=list)  # [{message, intent}] exemplars, auto-tuned from feedback


class Config(BaseModel):
    model_config = ConfigDict(extra="ignore")  # tolerate unmodeled sections (improvement, privacy, ...)
    rules: RulesConfig = Field(default_factory=RulesConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    modules: ModulesConfig = Field(default_factory=ModulesConfig)
    feedback: FeedbackConfig = Field(default_factory=FeedbackConfig)
    classify: ClassifyConfig = Field(default_factory=ClassifyConfig)


_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def _resolve(path: Optional[str]) -> Path:
    if path:
        return Path(path)
    active = _CONFIG_DIR / "config.yaml"
    return active if active.exists() else _CONFIG_DIR / "config.example.yaml"


def load_config(path: Optional[str] = None) -> Config:
    data = yaml.safe_load(_resolve(path).read_text(encoding="utf-8")) or {}
    return Config(**data)
