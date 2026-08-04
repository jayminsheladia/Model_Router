import os
import time
from dataclasses import dataclass
from typing import Optional

import groq

from app.models.mock_client import ModelUnavailableError, outage_marker_targets, strip_outage_marker
from app.schemas import Tier

MAX_COMPLETION_TOKENS = 600
SYSTEM_PROMPT = "You are a helpful coding assistant. Be concise and direct."


@dataclass
class TierSpec:
    model_name: str
    input_price_per_1m: float
    output_price_per_1m: float


TIER_SPECS: dict[Tier, TierSpec] = {
    Tier.CHEAP: TierSpec("llama-3.1-8b-instant", 0.05, 0.08),
    Tier.MID: TierSpec("openai/gpt-oss-20b", 0.075, 0.30),
    Tier.FRONTIER: TierSpec("openai/gpt-oss-120b", 0.15, 0.60),
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
