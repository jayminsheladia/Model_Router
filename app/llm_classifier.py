from __future__ import annotations

import os
from typing import Optional

import anthropic

from app.schemas import ClassifierOutput, Tier

MODEL = "claude-haiku-4-5"
REQUEST_TIMEOUT_SECONDS = 5.0

COMPLEXITY_TO_TIER = {
    "low": Tier.CHEAP,
    "medium": Tier.MID,
    "high": Tier.FRONTIER,
}

SYSTEM_PROMPT = (
    "You are a complexity classifier for a coding-assistant request router. "
    "Given the user's request, respond with exactly one word on the first line: "
    "low, medium, or high - indicating whether it needs a cheap/fast model (low), "
    "a mid-tier model (medium), or a frontier/most-capable model (high). "
    "On the second line, give a one-sentence reason."
)


def _is_ambiguous(heuristic: ClassifierOutput) -> bool:
    return heuristic.complexity == "medium" and not heuristic.matched_keywords


class LLMClassifier:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY")
        self.enabled = bool(self.api_key)
        self._client = anthropic.Anthropic(api_key=self.api_key) if self.enabled else None

    def reclassify_if_ambiguous(self, prompt: str, heuristic: ClassifierOutput) -> ClassifierOutput:
        if not self.enabled or heuristic.off_policy or not _is_ambiguous(heuristic):
            return heuristic

        try:
            response = self._client.with_options(timeout=REQUEST_TIMEOUT_SECONDS).messages.create(
                model=MODEL,
                max_tokens=40,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            text = next(block.text for block in response.content if block.type == "text")
            first_line = text.strip().splitlines()[0].strip().lower()
            new_tier = COMPLEXITY_TO_TIER.get(first_line)
            if new_tier is None or new_tier == heuristic.suggested_tier:
                return heuristic

            reasoning = (
                f"{heuristic.reasoning} Heuristic was ambiguous; {MODEL} "
                f"classified this as {first_line} complexity, overriding the default."
            )
            return heuristic.model_copy(
                update={
                    "complexity": first_line,
                    "suggested_tier": new_tier,
                    "reasoning": reasoning,
                }
            )
        except Exception:
            return heuristic
