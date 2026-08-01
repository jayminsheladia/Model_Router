import random
import re
from dataclasses import dataclass

from app.schemas import Tier

TOKENS_PER_CHAR = 0.25  # rough char->token estimate for a mock backend

OUTAGE_MARKER_PATTERN = re.compile(r"\[\[simulate-outage:(cheap|mid|frontier)\]\]")


class ModelUnavailableError(Exception):
    def __init__(self, tier: Tier):
        self.tier = tier
        super().__init__(f"Model tier '{tier}' is unavailable")


@dataclass
class TierSpec:
    model_name: str
    cost_per_1k_tokens: float
    latency_ms_range: tuple[float, float]


TIER_SPECS: dict[Tier, TierSpec] = {
    Tier.CHEAP: TierSpec("mock-cheap-mini", 0.15, (100, 300)),
    Tier.MID: TierSpec("mock-mid-standard", 1.00, (300, 800)),
    Tier.FRONTIER: TierSpec("mock-frontier-max", 6.00, (800, 2000)),
}


class MockModelClient:
    def __init__(self, force_fail_tiers: frozenset = frozenset()):
        self.force_fail_tiers = set(force_fail_tiers)

    def call(self, tier: Tier, prompt: str) -> tuple[str, int, float, float]:
        if tier in self.force_fail_tiers or self._outage_marker_targets(prompt, tier):
            raise ModelUnavailableError(tier)

        spec = TIER_SPECS[tier]
        tokens = max(1, int(len(prompt) * TOKENS_PER_CHAR))
        cost_usd = (tokens / 1000) * spec.cost_per_1k_tokens
        latency_ms = random.uniform(*spec.latency_ms_range)
        output_text = f"[{spec.model_name}] mock response ({tokens} tokens)"
        return output_text, tokens, cost_usd, latency_ms

    @staticmethod
    def _outage_marker_targets(prompt: str, tier: Tier) -> bool:
        match = OUTAGE_MARKER_PATTERN.search(prompt)
        return match is not None and match.group(1) == tier.value
