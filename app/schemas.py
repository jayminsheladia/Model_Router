from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel


class Tier(str, Enum):
    CHEAP = "cheap"
    MID = "mid"
    FRONTIER = "frontier"


TIER_ORDER = [Tier.CHEAP, Tier.MID, Tier.FRONTIER]


class RouteRequest(BaseModel):
    user_id: str
    prompt: str
    project: Optional[str] = None
    routing_mode: Literal["quality", "cost", "balanced"] = "balanced"


class ClassifierOutput(BaseModel):
    complexity: str  # "low" | "medium" | "high"
    suggested_tier: Tier
    off_policy: bool
    reasoning: str
    matched_keywords: list[str]


class BudgetState(BaseModel):
    spent_usd: float
    limit_usd: float
    over_soft_limit: bool
    over_hard_limit: bool


class PolicyDecision(BaseModel):
    allowed: bool
    final_tier: Optional[Tier]
    reason: str


class RouteResponse(BaseModel):
    audit_id: int
    allowed: bool
    final_tier: Optional[Tier]
    model_name: Optional[str]
    output_text: Optional[str]
    cost_usd: float
    latency_ms: float
    reason: str
    classifier: ClassifierOutput
    budget: BudgetState


class FeedbackRequest(BaseModel):
    audit_id: int
    signal: Literal["escalate", "downgrade_ok"]


class FeedbackResponse(BaseModel):
    ok: bool
    audit_id: int
    signal: str


class UserSummary(BaseModel):
    user_id: str
    team: str
    max_tier: Tier
    allowed_projects: list[str]
    project_tier_overrides: dict[str, Tier]
    budget: BudgetState
