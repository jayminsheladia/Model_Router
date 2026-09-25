import sqlite3
import threading
from pathlib import Path

import pytest

from app.audit import AuditLogger
from app.budget import BudgetLedger
from app.conversations import MAX_HISTORY_MESSAGES, ConversationAccessError, ConversationStore
from app.feedback import FeedbackStore
from app.identity import IdentityRegistry
from app.llm_classifier import LLMClassifier
from app.models.mock_client import MockModelClient, ModelUnavailableError
from app.projects import ProjectRegistry
from app.router import (
    AuditNotFoundError,
    InvalidFeedbackError,
    RouteOrchestrator,
    UnknownUserError,
)
from app.schemas import RouteRequest, Tier

HIGH_COMPLEXITY_PROMPT = "design the architecture for a distributed migration system"
MEDIUM_COMPLEXITY_PROMPT = "update the config for the staging environment"


@pytest.fixture
def orchestrator(tmp_path: Path) -> RouteOrchestrator:
    db_path = tmp_path / "router.db"
    return RouteOrchestrator(
        identity=IdentityRegistry(db_path=db_path),
        budget=BudgetLedger(db_path=db_path),
        model_client=MockModelClient(),
        audit=AuditLogger(db_path=db_path),
        feedback=FeedbackStore(db_path=db_path),
        project_registry=ProjectRegistry(db_path=db_path),
        # Force-disabled regardless of the host shell's env, so tests never make a
        # real network call even if ANTHROPIC_API_KEY happens to be set locally.
        llm_classifier=LLMClassifier(api_key=None),
        conversations=ConversationStore(db_path=db_path),
    )


def test_normal_request_is_allowed(orchestrator: RouteOrchestrator):
    response = orchestrator.route(
        RouteRequest(user_id="alice", prompt="refactor this function to be async")
    )
    assert response.allowed is True
    assert response.final_tier is not None
    assert response.cost_usd > 0
    assert response.audit_id > 0


def test_off_policy_request_is_blocked(orchestrator: RouteOrchestrator):
    response = orchestrator.route(
        RouteRequest(user_id="alice", prompt="write my personal cover letter for a job")
    )
    assert response.allowed is False
    assert response.final_tier is None
    assert "off-policy" in response.reason.lower()


def test_low_budget_user_gets_downgraded_then_blocked(orchestrator: RouteOrchestrator):
    user = orchestrator.identity.get_user("bob")

    first = orchestrator.route(RouteRequest(user_id="bob", prompt=HIGH_COMPLEXITY_PROMPT))
    assert first.allowed is True
    assert first.final_tier == Tier.MID  # capped: bob's max_tier is mid

    # Push bob over the soft limit (80% of his $1.00 budget) and confirm a downgrade.
    orchestrator.budget.record(user, 0.9)
    second = orchestrator.route(RouteRequest(user_id="bob", prompt=HIGH_COMPLEXITY_PROMPT))
    assert second.allowed is True
    assert second.final_tier == Tier.CHEAP
    assert "downgraded" in second.reason.lower()

    # Push bob over the hard limit and confirm the request is blocked outright.
    orchestrator.budget.record(user, 0.5)
    third = orchestrator.route(RouteRequest(user_id="bob", prompt=HIGH_COMPLEXITY_PROMPT))
    assert third.allowed is False
    assert "budget" in third.reason.lower()


def test_unknown_user_raises(orchestrator: RouteOrchestrator):
    with pytest.raises(UnknownUserError):
        orchestrator.route(RouteRequest(user_id="nobody", prompt="hello"))


def test_audit_log_records_entries(orchestrator: RouteOrchestrator):
    orchestrator.route(RouteRequest(user_id="alice", prompt="explain what this does"))
    recent = orchestrator.audit.read_recent(5)
    assert len(recent) == 1
    assert recent[0]["user_id"] == "alice"


def test_project_required_block(orchestrator: RouteOrchestrator):
    response = orchestrator.route(RouteRequest(user_id="carol", prompt="fix the bug in checkout"))
    assert response.allowed is False
    assert "project" in response.reason.lower()


def test_project_not_authorized_block(orchestrator: RouteOrchestrator):
    response = orchestrator.route(
        RouteRequest(user_id="carol", prompt="fix the bug in checkout", project="some-other-repo")
    )
    assert response.allowed is False
    assert "not authorized" in response.reason.lower()


def test_project_tier_override_raises_cap(orchestrator: RouteOrchestrator):
    overridden = orchestrator.route(
        RouteRequest(user_id="carol", prompt=HIGH_COMPLEXITY_PROMPT, project="internal-tools")
    )
    assert overridden.allowed is True
    assert overridden.final_tier == Tier.FRONTIER  # per-project override raises carol's usual mid cap

    default_capped = orchestrator.route(
        RouteRequest(user_id="carol", prompt=HIGH_COMPLEXITY_PROMPT, project="growth-experiments")
    )
    assert default_capped.allowed is True
    assert default_capped.final_tier == Tier.MID  # falls back to carol's regular max_tier


def test_feedback_loop_bumps_classifier_suggestion(orchestrator: RouteOrchestrator):
    first = orchestrator.route(RouteRequest(user_id="alice", prompt=MEDIUM_COMPLEXITY_PROMPT))
    second = orchestrator.route(RouteRequest(user_id="alice", prompt=MEDIUM_COMPLEXITY_PROMPT))
    assert first.classifier.complexity == "medium"
    assert first.final_tier == Tier.MID
    assert second.final_tier == Tier.MID

    orchestrator.record_feedback(first.audit_id, "escalate")
    orchestrator.record_feedback(second.audit_id, "escalate")

    third = orchestrator.route(RouteRequest(user_id="alice", prompt=MEDIUM_COMPLEXITY_PROMPT))
    assert third.final_tier == Tier.FRONTIER
    assert "bumped" in third.classifier.reasoning.lower()


def test_feedback_rejected_on_blocked_request(orchestrator: RouteOrchestrator):
    blocked = orchestrator.route(
        RouteRequest(user_id="alice", prompt="write my personal cover letter")
    )
    with pytest.raises(InvalidFeedbackError):
        orchestrator.record_feedback(blocked.audit_id, "escalate")


def test_feedback_unknown_audit_id_raises(orchestrator: RouteOrchestrator):
    with pytest.raises(AuditNotFoundError):
        orchestrator.record_feedback(99999, "escalate")


def test_project_registry_blocks_unauthorized_user_even_when_unrestricted(
    orchestrator: RouteOrchestrator,
):
    # alice has no user-level allowed_projects (unrestricted), but "internal-tools" is
    # registered with only carol authorized -- the project-level check must still block her.
    response = orchestrator.route(
        RouteRequest(user_id="alice", prompt="fix a bug", project="internal-tools")
    )
    assert response.allowed is False
    assert "not authorized" in response.reason.lower()
    assert "project-level restriction" in response.reason.lower()


def test_project_registry_allows_authorized_user(orchestrator: RouteOrchestrator):
    response = orchestrator.route(
        RouteRequest(user_id="carol", prompt="fix a bug", project="internal-tools")
    )
    assert response.allowed is True


def test_unregistered_project_is_open(orchestrator: RouteOrchestrator):
    response = orchestrator.route(
        RouteRequest(user_id="alice", prompt="fix a bug", project="some-brand-new-project")
    )
    assert response.allowed is True


def test_routing_mode_cost_downgrades_below_balanced(orchestrator: RouteOrchestrator):
    balanced = orchestrator.route(
        RouteRequest(user_id="alice", prompt=HIGH_COMPLEXITY_PROMPT, routing_mode="balanced")
    )
    cost = orchestrator.route(
        RouteRequest(user_id="alice", prompt=HIGH_COMPLEXITY_PROMPT, routing_mode="cost")
    )
    assert balanced.final_tier == Tier.FRONTIER
    assert cost.final_tier == Tier.MID  # cost mode pre-emptively steps down one tier
    assert "cost mode" in cost.reason.lower()


def test_routing_mode_quality_ignores_soft_budget_downgrade(orchestrator: RouteOrchestrator):
    user = orchestrator.identity.get_user("bob")
    orchestrator.budget.record(user, 0.9)  # over bob's 80% soft limit

    balanced = orchestrator.route(
        RouteRequest(user_id="bob", prompt=HIGH_COMPLEXITY_PROMPT, routing_mode="balanced")
    )
    quality = orchestrator.route(
        RouteRequest(user_id="bob", prompt=HIGH_COMPLEXITY_PROMPT, routing_mode="quality")
    )
    assert balanced.final_tier == Tier.CHEAP  # downgraded from the usual mid cap
    assert quality.final_tier == Tier.MID  # quality mode ignores the soft-limit downgrade
    assert "quality mode" in quality.reason.lower()


def test_failover_steps_down_to_next_tier(tmp_path: Path):
    db_path = tmp_path / "router.db"
    orchestrator = RouteOrchestrator(
        identity=IdentityRegistry(db_path=db_path),
        budget=BudgetLedger(db_path=db_path),
        model_client=MockModelClient(force_fail_tiers=frozenset({Tier.FRONTIER})),
        audit=AuditLogger(db_path=db_path),
        feedback=FeedbackStore(db_path=db_path),
        project_registry=ProjectRegistry(db_path=db_path),
        llm_classifier=LLMClassifier(api_key=None),
        conversations=ConversationStore(db_path=db_path),
    )
    response = orchestrator.route(RouteRequest(user_id="alice", prompt=HIGH_COMPLEXITY_PROMPT))
    assert response.allowed is True
    assert response.final_tier == Tier.MID
    assert "failover" in response.reason.lower()
    assert "frontier" in response.reason.lower()


def test_simulate_outage_marker_triggers_failover(orchestrator: RouteOrchestrator):
    prompt = HIGH_COMPLEXITY_PROMPT + " [[simulate-outage:frontier]]"
    response = orchestrator.route(RouteRequest(user_id="alice", prompt=prompt))
    assert response.allowed is True
    assert response.final_tier == Tier.MID
    assert "failover" in response.reason.lower()


def test_mock_client_raises_for_forced_tier():
    client = MockModelClient(force_fail_tiers=frozenset({Tier.CHEAP}))
    with pytest.raises(ModelUnavailableError):
        client.call(Tier.CHEAP, "hello")


def test_llm_classifier_disabled_is_a_noop():
    from app.classifier import classify

    heuristic = classify(MEDIUM_COMPLEXITY_PROMPT)
    llm = LLMClassifier(api_key=None)
    assert llm.enabled is False

    result = llm.reclassify_if_ambiguous(MEDIUM_COMPLEXITY_PROMPT, heuristic)
    assert result == heuristic


def test_groq_client_disabled_without_key(monkeypatch):
    from app.models.groq_client import GroqModelClient

    # Other test modules import app.main, whose module-level load_dotenv() call
    # can leak a real GROQ_API_KEY from .env into this process's environment --
    # force it unset so this test reflects "no key provided" regardless of
    # what already ran earlier in the same pytest session.
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    client = GroqModelClient(api_key=None)
    assert client.enabled is False


def test_orchestrator_falls_back_to_mock_when_groq_key_unset(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    db_path = tmp_path / "router.db"
    orchestrator = RouteOrchestrator(
        identity=IdentityRegistry(db_path=db_path),
        budget=BudgetLedger(db_path=db_path),
        audit=AuditLogger(db_path=db_path),
        feedback=FeedbackStore(db_path=db_path),
        project_registry=ProjectRegistry(db_path=db_path),
        llm_classifier=LLMClassifier(api_key=None),
        conversations=ConversationStore(db_path=db_path),
        # model_client intentionally omitted -> exercises the auto-select logic
    )
    assert isinstance(orchestrator.model_client, MockModelClient)


def test_groq_client_maps_api_error_to_model_unavailable(monkeypatch):
    import groq
    import httpx

    from app.models.groq_client import GroqModelClient

    client = GroqModelClient(api_key="fake-key-for-test")

    def raise_connection_error(*args, **kwargs):
        raise groq.APIConnectionError(
            message="boom", request=httpx.Request("POST", "https://api.groq.com")
        )

    monkeypatch.setattr(client._client.chat.completions, "create", raise_connection_error)

    with pytest.raises(ModelUnavailableError):
        client.call(Tier.CHEAP, "hello")


class RecordingModelClient(MockModelClient):
    """MockModelClient that records the `history` it was called with, for asserting
    that conversation memory actually threads prior turns into the model call."""

    def __init__(self):
        super().__init__()
        self.received_histories: list[list[dict] | None] = []

    def call(self, tier, prompt, history=None):
        self.received_histories.append(history)
        return super().call(tier, prompt, history)


def _orchestrator_with_recording_client(tmp_path: Path) -> tuple[RouteOrchestrator, RecordingModelClient]:
    db_path = tmp_path / "router.db"
    client = RecordingModelClient()
    orchestrator = RouteOrchestrator(
        identity=IdentityRegistry(db_path=db_path),
        budget=BudgetLedger(db_path=db_path),
        model_client=client,
        audit=AuditLogger(db_path=db_path),
        feedback=FeedbackStore(db_path=db_path),
        project_registry=ProjectRegistry(db_path=db_path),
        llm_classifier=LLMClassifier(api_key=None),
        conversations=ConversationStore(db_path=db_path),
    )
    return orchestrator, client


def test_first_turn_mints_a_conversation_id(tmp_path: Path):
    orchestrator, _client = _orchestrator_with_recording_client(tmp_path)
    response = orchestrator.route(RouteRequest(user_id="alice", prompt="hello there"))
    assert response.allowed is True
    assert response.conversation_id is not None


def test_second_turn_threads_prior_history_into_model_call(tmp_path: Path):
    orchestrator, client = _orchestrator_with_recording_client(tmp_path)
    first = orchestrator.route(RouteRequest(user_id="alice", prompt="remember the number 42"))
    assert client.received_histories[0] in (None, [])  # first turn: no prior history

    orchestrator.route(
        RouteRequest(
            user_id="alice",
            prompt="what number did I just tell you?",
            conversation_id=first.conversation_id,
        )
    )
    second_call_history = client.received_histories[1]
    assert second_call_history is not None
    assert len(second_call_history) == 2  # prior user turn + prior assistant turn
    assert second_call_history[0]["role"] == "user"
    assert "42" in second_call_history[0]["content"]
    assert second_call_history[1]["role"] == "assistant"


def test_blocked_turn_does_not_extend_conversation(orchestrator: RouteOrchestrator):
    first = orchestrator.route(RouteRequest(user_id="alice", prompt="hello there"))
    blocked = orchestrator.route(
        RouteRequest(
            user_id="alice",
            prompt="write my personal cover letter",
            conversation_id=first.conversation_id,
        )
    )
    assert blocked.allowed is False
    assert blocked.conversation_id is None

    history = orchestrator.conversations.get_history(first.conversation_id, "alice")
    assert len(history) == 2  # unchanged: just the first turn's user+assistant messages


def test_continuing_conversation_as_different_user_is_blocked(orchestrator: RouteOrchestrator):
    first = orchestrator.route(RouteRequest(user_id="alice", prompt="hello there"))
    hijack_attempt = orchestrator.route(
        RouteRequest(user_id="bob", prompt="what did alice say?", conversation_id=first.conversation_id)
    )
    assert hijack_attempt.allowed is False
    assert "does not belong to" in hijack_attempt.reason.lower()


def test_conversation_store_raises_on_cross_user_access(tmp_path: Path):
    store = ConversationStore(db_path=tmp_path / "router.db")
    store.append("conv-1", "alice", "user", "hi")
    store.append("conv-1", "alice", "assistant", "hello")

    assert len(store.get_history("conv-1", "alice")) == 2
    with pytest.raises(ConversationAccessError):
        store.get_history("conv-1", "bob")


# --- Budget: monthly window, reservations, concurrency ---


def test_budget_only_counts_the_current_month(tmp_path: Path):
    """Spend from a previous month must not count against this month's limit."""
    db_path = tmp_path / "router.db"
    ledger = BudgetLedger(db_path=db_path)
    user = IdentityRegistry(db_path=db_path).get_user("bob")  # $1.00 limit

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO usage (user_id, cost_usd, created_at, pending) VALUES (?, ?, ?, 0)",
            ("bob", 5.00, "2020-01-15T00:00:00+00:00"),
        )

    state = ledger.check(user)
    assert state.spent_usd == 0.0
    assert state.over_hard_limit is False


def test_budget_counts_spend_inside_the_current_month(tmp_path: Path):
    db_path = tmp_path / "router.db"
    ledger = BudgetLedger(db_path=db_path)
    user = IdentityRegistry(db_path=db_path).get_user("bob")

    ledger.record(user, 0.90)
    state = ledger.check(user)
    assert state.spent_usd == pytest.approx(0.90)
    assert state.over_soft_limit is True
    assert state.over_hard_limit is False


def test_reservation_is_visible_to_a_concurrent_request(tmp_path: Path):
    """An in-flight reservation counts against the budget before it settles."""
    db_path = tmp_path / "router.db"
    ledger = BudgetLedger(db_path=db_path)
    user = IdentityRegistry(db_path=db_path).get_user("bob")  # $1.00 limit

    first = ledger.reserve(user, 1.50)
    assert first is not None
    # Second request sees the first one's worst-case spend and is refused.
    assert ledger.reserve(user, 1.50) is None

    # Settling down to the real cost frees the headroom again.
    ledger.settle(first, 0.10)
    assert ledger.reserve(user, 0.10) is not None


def test_released_reservation_frees_budget(tmp_path: Path):
    db_path = tmp_path / "router.db"
    ledger = BudgetLedger(db_path=db_path)
    user = IdentityRegistry(db_path=db_path).get_user("bob")

    reservation = ledger.reserve(user, 1.50)
    ledger.release(reservation)
    assert ledger.check(user).spent_usd == 0.0


def test_concurrent_requests_cannot_overshoot_the_hard_limit(tmp_path: Path):
    """The bug this guards: N threads all read the same pre-spend total,
    all pass the limit check, and together blow past the cap."""
    db_path = tmp_path / "router.db"
    ledger = BudgetLedger(db_path=db_path)
    user = IdentityRegistry(db_path=db_path).get_user("bob")  # $1.00 limit

    granted = []
    barrier = threading.Barrier(8)

    def attempt():
        barrier.wait()
        if ledger.reserve(user, 1.00) is not None:
            granted.append(1)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(granted) == 1, f"{len(granted)} requests passed a $1.00 cap reserving $1.00 each"


# --- Conversation history is bounded ---


def test_history_is_capped_to_the_most_recent_turns(tmp_path: Path):
    store = ConversationStore(db_path=tmp_path / "router.db")
    for i in range(40):
        store.append("conv-1", "alice", "user", f"message {i}")

    history = store.get_history("conv-1", "alice")
    assert len(history) == MAX_HISTORY_MESSAGES
    # Keeps the newest turns, not the oldest.
    assert history[-1]["content"] == "message 39"


def test_long_conversation_does_not_grow_cost_without_bound(tmp_path: Path):
    """Per-turn prompt cost must plateau once history hits the window."""
    db_path = tmp_path / "router.db"
    orchestrator = RouteOrchestrator(
        identity=IdentityRegistry(db_path=db_path),
        budget=BudgetLedger(db_path=db_path),
        model_client=MockModelClient(),
        audit=AuditLogger(db_path=db_path),
        feedback=FeedbackStore(db_path=db_path),
        project_registry=ProjectRegistry(db_path=db_path),
        llm_classifier=LLMClassifier(api_key=None),
        conversations=ConversationStore(db_path=db_path),
    )

    conversation_id = None
    costs = []
    for i in range(24):
        response = orchestrator.route(
            RouteRequest(
                user_id="alice",
                prompt="refactor this function to be async",
                conversation_id=conversation_id,
            )
        )
        conversation_id = response.conversation_id
        costs.append(response.cost_usd)

    assert costs[-1] == pytest.approx(costs[-2])
