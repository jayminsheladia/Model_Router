"""
Benchmarks the heuristic classifier (app/classifier.py) against a small,
hand-labeled "oracle" eval set, using REAL Groq API calls for cost/latency
(not estimated).

This measures the classifier in isolation from the policy/budget layer --
it calls app.classifier.classify() and app.models.groq_client.GroqModelClient
directly, bypassing RouteOrchestrator entirely, so the reported numbers are
about classification quality and its real dollar impact, not about the
budget/project-authorization logic (which is covered by tests/test_router.py
instead).

Usage:
    cp .env.example .env   # set GROQ_API_KEY
    python scripts/benchmark_classifier.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from app.classifier import classify
from app.models.groq_client import GroqModelClient
from app.schemas import Tier

# 30 realistic coding-assistant prompts, 10 each hand-labeled cheap/mid/frontier
# by judging what a competent engineer would actually need for that request.
EVAL_SET = [
    ("what does len() do in python, quick question", "cheap"),
    ("rename variable x to count in this snippet", "cheap"),
    ("fix this typo: 'recieve' should be 'receive'", "cheap"),
    ("what is the capital of France", "cheap"),
    ("explain what a for loop does, one liner", "cheap"),
    ('is this valid JSON: {"a": 1}', "cheap"),
    ("what does HTTP 404 mean, quick question", "cheap"),
    ("simple question: what's 15 percent of 200", "cheap"),
    ("one-liner: how do I reverse a string in python", "cheap"),
    ("what does the git status command do", "cheap"),
    ("refactor this function to be async", "mid"),
    ("write unit tests for a function that validates email addresses", "mid"),
    ("review this pull request diff for potential bugs", "mid"),
    ("update the database config for the staging environment", "mid"),
    ("add error handling to this API endpoint", "mid"),
    ("explain why this SQL query is slow", "mid"),
    ("write a Dockerfile for a Node.js app", "mid"),
    ("add input validation to this form handler", "mid"),
    ("convert this callback-based code to use promises", "mid"),
    ("debug why this test is intermittently failing", "mid"),
    ("design the architecture for a distributed migration system across multiple regions", "frontier"),
    ("design a multi-tenant database schema with row-level security for a SaaS platform", "frontier"),
    ("architect a real-time event-driven system handling millions of messages per day", "frontier"),
    ("do a security review of this authentication and authorization flow", "frontier"),
    ("design a disaster recovery and failover strategy for a distributed database cluster", "frontier"),
    ("refactor this monolith into microservices with clear service boundaries", "frontier"),
    ("design a rate-limiting and backpressure system for a high-throughput API gateway", "frontier"),
    ("architect a data pipeline for real-time fraud detection at scale", "frontier"),
    ("design a zero-downtime deployment strategy for a stateful distributed system", "frontier"),
    ("do a full performance and scalability audit of this distributed caching layer", "frontier"),
]

TIER_MAP = {"cheap": Tier.CHEAP, "mid": Tier.MID, "frontier": Tier.FRONTIER}


def main():
    client = GroqModelClient()
    if not client.enabled:
        raise SystemExit("GROQ_API_KEY not set -- copy .env.example to .env and add a key.")

    missing = client.verify_models()
    if missing:
        raise SystemExit(f"Configured models no longer served by Groq: {', '.join(missing)}")

    correct = 0
    predicted_cost_total = 0.0
    frontier_cost_total = 0.0
    latencies = []
    confusion: dict[tuple[str, str], int] = {}

    for i, (prompt, oracle) in enumerate(EVAL_SET, 1):
        predicted = classify(prompt).suggested_tier.value
        is_correct = predicted == oracle
        correct += int(is_correct)
        confusion[(oracle, predicted)] = confusion.get((oracle, predicted), 0) + 1

        _, _, pred_cost, pred_latency, _ = client.call(TIER_MAP[predicted], prompt)
        predicted_cost_total += pred_cost
        latencies.append(pred_latency)

        frontier_cost = pred_cost if predicted == "frontier" else client.call(Tier.FRONTIER, prompt)[2]
        frontier_cost_total += frontier_cost

        mark = "OK  " if is_correct else "MISS"
        print(f"[{i:2}/{len(EVAL_SET)}] {mark} oracle={oracle:8} predicted={predicted:8} "
              f"cost=${pred_cost:.6f}  {prompt[:55]}")
        time.sleep(0.05)

    accuracy = correct / len(EVAL_SET)
    savings_pct = (1 - predicted_cost_total / frontier_cost_total) * 100 if frontier_cost_total else 0

    print("\n" + "=" * 70)
    print(f"Accuracy vs oracle:              {correct}/{len(EVAL_SET)} ({accuracy*100:.1f}%)")
    print(f"Total cost (router):             ${predicted_cost_total:.6f}")
    print(f"Total cost (always-frontier):    ${frontier_cost_total:.6f}")
    print(f"Cost savings vs always-frontier: {savings_pct:.1f}%")
    print(f"Avg real latency per request:    {sum(latencies)/len(latencies):.0f}ms")
    print("\nConfusion (oracle -> predicted):")
    for (oracle, predicted), count in sorted(confusion.items()):
        flag = "" if oracle == predicted else "  <-- misclassified"
        print(f"  {oracle:8} -> {predicted:8}: {count}{flag}")


if __name__ == "__main__":
    main()
