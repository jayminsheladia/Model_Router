"""
Measures what the classifier benchmark can't: whether routing *down* actually
costs you anything.

`benchmark_classifier.py` scores the classifier against hand-written tier labels.
That answers "did the heuristic agree with me?", not "was the cheaper answer good
enough?" -- so a 100% score there is compatible with routing every prompt to a
model that answers badly. This script closes that gap by pairing each routed
answer against the frontier answer for the same prompt and having a judge model
pick a winner, blind to which is which.

Method:
  - route the prompt, call the tier the router chose
  - call the frontier tier on the same prompt
  - show both answers to a judge in randomized order, ask which better serves
    the request (or TIE)
  - a routed answer "holds quality" if the judge prefers it or calls a tie

Position order is randomized per prompt because judge models favour whichever
answer they read first. Prompts the router already sends to frontier are counted
as holding quality without spending a judge call -- the two answers would be
drawn from the same model.

Usage:
    cp .env.example .env   # set GROQ_API_KEY
    python scripts/benchmark_quality.py [n_prompts]
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from app.classifier import classify
from app.models.groq_client import TIER_SPECS, GroqModelClient
from app.schemas import Tier
from scripts.benchmark_classifier import EVAL_SET

JUDGE_TIER = Tier.FRONTIER

JUDGE_PROMPT = """You are grading two answers to the same request.

REQUEST:
{prompt}

ANSWER A:
{answer_a}

ANSWER B:
{answer_b}

Which answer better serves the request? Judge only on usefulness, correctness and
appropriate depth. Ignore length, formatting and tone unless they harm usefulness.
If the two are equally useful, say TIE.

Reply with exactly one word: A, B, or TIE."""


def judge(client: GroqModelClient, prompt: str, routed_answer: str, frontier_answer: str):
    """Returns (routed_held_quality, verdict_label, judge_cost)."""
    routed_first = random.random() < 0.5
    answer_a, answer_b = (
        (routed_answer, frontier_answer) if routed_first else (frontier_answer, routed_answer)
    )

    raw, _, cost, _, _ = client.call(
        JUDGE_TIER,
        JUDGE_PROMPT.format(prompt=prompt, answer_a=answer_a, answer_b=answer_b),
    )
    verdict = (raw or "").strip().upper()
    choice = next((tok for tok in ("TIE", "A", "B") if verdict.startswith(tok)), "TIE")

    if choice == "TIE":
        return True, "tie", cost
    routed_won = (choice == "A") == routed_first
    return routed_won, "routed" if routed_won else "frontier", cost


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else len(EVAL_SET)
    eval_set = EVAL_SET[:limit]

    client = GroqModelClient()
    if not client.enabled:
        raise SystemExit("GROQ_API_KEY not set -- copy .env.example to .env and add a key.")

    missing = client.verify_models()
    if missing:
        raise SystemExit(f"Configured models no longer served by Groq: {', '.join(missing)}")

    random.seed(0)
    held = 0
    routed_cost_total = 0.0
    frontier_cost_total = 0.0
    judge_cost_total = 0.0
    verdicts: dict[str, int] = {}
    regressions = []

    for i, (prompt, _oracle) in enumerate(eval_set, 1):
        tier = classify(prompt).suggested_tier
        routed_answer, _, routed_cost, _, _ = client.call(tier, prompt)
        routed_cost_total += routed_cost

        if tier is Tier.FRONTIER:
            frontier_cost_total += routed_cost
            held += 1
            verdicts["same-tier"] = verdicts.get("same-tier", 0) + 1
            print(f"[{i:2}/{len(eval_set)}] HOLD  tier={tier.value:8} (already frontier)  {prompt[:44]}")
            continue

        frontier_answer, _, frontier_cost, _, _ = client.call(Tier.FRONTIER, prompt)
        frontier_cost_total += frontier_cost

        ok, label, judge_cost = judge(client, prompt, routed_answer, frontier_answer)
        judge_cost_total += judge_cost
        held += int(ok)
        verdicts[label] = verdicts.get(label, 0) + 1
        if not ok:
            regressions.append((tier.value, prompt))

        print(f"[{i:2}/{len(eval_set)}] {'HOLD' if ok else 'LOSS'}  tier={tier.value:8} "
              f"judge={label:8}  {prompt[:44]}")

    n = len(eval_set)
    quality_pct = held / n * 100
    savings_pct = (1 - routed_cost_total / frontier_cost_total) * 100 if frontier_cost_total else 0

    print("\n" + "=" * 72)
    print(f"Prompts evaluated:                {n}")
    print(f"Quality held vs. frontier:        {held}/{n} ({quality_pct:.1f}%)")
    print(f"Cost (router):                    ${routed_cost_total:.6f}")
    print(f"Cost (always-frontier):           ${frontier_cost_total:.6f}")
    print(f"Cost savings vs always-frontier:  {savings_pct:.1f}%")
    print(f"Judge overhead (offline only):    ${judge_cost_total:.6f}")
    print("\nJudge verdicts:")
    for label, count in sorted(verdicts.items()):
        print(f"  {label:10}: {count}")
    if regressions:
        print("\nPrompts where the cheaper tier lost:")
        for tier, prompt in regressions:
            print(f"  [{tier}] {prompt[:66]}")
    print(f"\nHeadline: {savings_pct:.1f}% cheaper at {quality_pct:.1f}% quality retention "
          f"(n={n}, single judge, pairwise).")


if __name__ == "__main__":
    main()
