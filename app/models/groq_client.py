import os
import time
from dataclasses import dataclass
from typing import Optional

import groq

from app.models.mock_client import ModelUnavailableError, outage_marker_targets, strip_outage_marker
from app.schemas import Tier

MAX_COMPLETION_TOKENS = 600
CHARS_PER_TOKEN = 4
SYSTEM_PROMPT = "You are a helpful coding assistant. Be concise and direct."


@dataclass
class TierSpec:
    model_name: str
    input_price_per_1m: float
    output_price_per_1m: float


# Prices are Groq's published per-1M-token rates. They are data, not trivia: the
# whole routing argument is that these three tiers cost materially different
# amounts, so a stale price here silently invalidates every cost claim.
TIER_SPECS: dict[Tier, TierSpec] = {
    Tier.CHEAP: TierSpec("openai/gpt-oss-20b", 0.075, 0.30),
    Tier.MID: TierSpec("openai/gpt-oss-120b", 0.15, 0.60),
    Tier.FRONTIER: TierSpec("qwen/qwen3.8-27b", 0.80, 4.00),
}


class GroqModelClient:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key if api_key is not None else os.environ.get("GROQ_API_KEY")
        self.enabled = bool(self.api_key)
        self._client = groq.Groq(api_key=self.api_key) if self.enabled else None

    def call(
        self, tier: Tier, prompt: str, history: Optional[list[dict]] = None
    ) -> tuple[str, int, float, float, str]:
        if outage_marker_targets(prompt, tier):
            raise ModelUnavailableError(tier)

        spec = TIER_SPECS[tier]
        clean_prompt = strip_outage_marker(prompt)

        start = time.monotonic()
        try:
            response = self._client.chat.completions.create(
                model=spec.model_name,
                max_tokens=MAX_COMPLETION_TOKENS,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    *(history or []),
                    {"role": "user", "content": clean_prompt},
                ],
            )
        except groq.APIError as exc:
            raise ModelUnavailableError(tier) from exc
        latency_ms = (time.monotonic() - start) * 1000

        output_text = response.choices[0].message.content
        prompt_tokens = response.usage.prompt_tokens
        completion_tokens = response.usage.completion_tokens
        cost_usd = (
            (prompt_tokens / 1_000_000) * spec.input_price_per_1m
            + (completion_tokens / 1_000_000) * spec.output_price_per_1m
        )

        return output_text, prompt_tokens + completion_tokens, cost_usd, latency_ms, spec.model_name

    def estimate_max_cost(
        self, tier: Tier, prompt: str, history: Optional[list[dict]] = None
    ) -> float:
        """Worst-case cost of one call, used to reserve budget before spending it.

        Output is billed at the full completion cap because the real length isn't
        known until the call returns; over-reserving is safe (the reservation is
        settled down to the actual cost), under-reserving is not.
        """
        history_chars = sum(len(m.get("content", "")) for m in (history or []))
        prompt_tokens = (len(prompt) + len(SYSTEM_PROMPT) + history_chars) / CHARS_PER_TOKEN
        spec = TIER_SPECS[tier]
        return (
            (prompt_tokens / 1_000_000) * spec.input_price_per_1m
            + (MAX_COMPLETION_TOKENS / 1_000_000) * spec.output_price_per_1m
        )

    def verify_models(self) -> list[str]:
        """Return the tiers whose configured model the API won't serve.

        Providers retire model IDs without warning, which otherwise surfaces as a
        404 in the middle of a run rather than at startup.
        """
        if not self.enabled:
            return []
        available = {m.id for m in self._client.models.list().data}
        return [
            f"{tier.value}={spec.model_name}"
            for tier, spec in TIER_SPECS.items()
            if spec.model_name not in available
        ]
