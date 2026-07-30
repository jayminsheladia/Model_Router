from pathlib import Path

import pytest

from app.audit import AuditLogger
from app.budget import BudgetLedger
from app.feedback import FeedbackStore
from app.identity import IdentityRegistry
from app.models.mock_client import MockModelClient
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
