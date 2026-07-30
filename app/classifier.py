from __future__ import annotations

import re
from typing import Callable, Optional

from app.schemas import TIER_ORDER, ClassifierOutput, Tier

HIGH_COMPLEXITY_KEYWORDS = {
    "architecture", "design", "migrate", "migration", "distributed",
    "scalability", "security review", "refactor the entire", "rewrite",
}
MULTI_FILE_KEYWORDS = {
    "across the codebase", "multi-file", "multiple files", "every module",
    "throughout the project",
}
LOW_COMPLEXITY_KEYWORDS = {
    "one-liner", "one liner", "typo", "rename", "explain", "what does",
    "simple", "quick question",
}
ROUTINE_TASK_KEYWORDS = {
    "generate tests", "write tests", "write documentation", "add docstrings",
    "format this",
}
OFF_POLICY_KEYWORDS = {
    "resume", "cover letter", "cv ", "personal essay", "my essay",
    "dating profile", "wedding speech", "birthday card", "personal use",
}

CODE_BLOCK_PATTERN = re.compile(r"```")

FEEDBACK_BUMP_THRESHOLD = 2

BiasLookup = Callable[[str], tuple[int, int]]


def _matches(text: str, keywords: set[str]) -> list[str]:
    return [keyword for keyword in keywords if keyword in text]


def classify(prompt: str, bias_lookup: Optional[BiasLookup] = None) -> ClassifierOutput:
    text = prompt.lower()

    off_policy_hits = _matches(text, OFF_POLICY_KEYWORDS)
    high_hits = _matches(text, HIGH_COMPLEXITY_KEYWORDS)
    multi_file_hits = _matches(text, MULTI_FILE_KEYWORDS)
    low_hits = _matches(text, LOW_COMPLEXITY_KEYWORDS)
    routine_hits = _matches(text, ROUTINE_TASK_KEYWORDS)

    has_code_block = bool(CODE_BLOCK_PATTERN.search(prompt))
    is_long = len(prompt) > 400

    matched_keywords = off_policy_hits + high_hits + multi_file_hits + low_hits + routine_hits

    if high_hits or multi_file_hits or (is_long and has_code_block):
        complexity = "high"
        suggested_tier = Tier.FRONTIER
        reasoning = "Matched high-complexity signal (architecture/multi-file keywords or long prompt with code)."
    elif routine_hits or (low_hits and not has_code_block):
        complexity = "low"
        suggested_tier = Tier.CHEAP
        reasoning = "Matched low-complexity signal (routine task or short/simple request, no code)."
    else:
        complexity = "medium"
        suggested_tier = Tier.MID
        reasoning = "No strong low- or high-complexity signal; defaulting to mid tier."

    if bias_lookup is not None:
        escalate, downgrade_ok = bias_lookup(complexity)
        net = escalate - downgrade_ok
        tier_idx = TIER_ORDER.index(suggested_tier)
        if net >= FEEDBACK_BUMP_THRESHOLD and tier_idx < len(TIER_ORDER) - 1:
            suggested_tier = TIER_ORDER[tier_idx + 1]
            reasoning += f" Bumped one tier from prior feedback ({escalate} escalate vs {downgrade_ok} downgrade-ok)."
        elif net <= -FEEDBACK_BUMP_THRESHOLD and tier_idx > 0:
            suggested_tier = TIER_ORDER[tier_idx - 1]
            reasoning += f" Dropped one tier from prior feedback ({escalate} escalate vs {downgrade_ok} downgrade-ok)."

    off_policy = bool(off_policy_hits)
    if off_policy:
        reasoning += " Off-policy keyword matched (looks like personal, non-work use)."

    return ClassifierOutput(
        complexity=complexity,
        suggested_tier=suggested_tier,
        off_policy=off_policy,
        reasoning=reasoning,
        matched_keywords=matched_keywords,
    )
