from __future__ import annotations

from app import classifier, policy
from app.audit import AuditLogger
from app.budget import BudgetLedger
from app.feedback import FeedbackStore
from app.identity import IdentityRegistry
from app.models.mock_client import MockModelClient
from app.projects import ProjectRegistry
from app.schemas import FeedbackResponse, RouteRequest, RouteResponse


class UnknownUserError(Exception):
    pass


class AuditNotFoundError(Exception):
    pass


class InvalidFeedbackError(Exception):
    pass


class RouteOrchestrator:
    def __init__(
        self,
        identity: IdentityRegistry | None = None,
        budget: BudgetLedger | None = None,
        model_client: MockModelClient | None = None,
        audit: AuditLogger | None = None,
        feedback: FeedbackStore | None = None,
        project_registry: ProjectRegistry | None = None,
    ):
        self.identity = identity or IdentityRegistry()
        self.budget = budget or BudgetLedger()
        self.model_client = model_client or MockModelClient()
        self.audit = audit or AuditLogger()
        self.feedback = feedback or FeedbackStore()
        self.project_registry = project_registry or ProjectRegistry()

    def route(self, request: RouteRequest) -> RouteResponse:
        user = self.identity.get_user(request.user_id)
        if user is None:
            raise UnknownUserError(f"Unknown user: {request.user_id}")

        classifier_output = classifier.classify(request.prompt, bias_lookup=self.feedback.get_bias)
        budget_state = self.budget.check(user)
        decision = policy.decide(
            user, request.project, classifier_output, budget_state, self.project_registry
        )

        if not decision.allowed:
            final_tier = None
            model_name = None
            output_text = None
            cost_usd = 0.0
            latency_ms = 0.0
        else:
            output_text, _tokens, cost_usd, latency_ms = self.model_client.call(
                decision.final_tier, request.prompt
            )
            self.budget.record(user, cost_usd)
            budget_state = self.budget.check(user)
            final_tier = decision.final_tier
            model_name = self.model_client.__class__.__name__

        audit_id = self.audit.record(
            {
                "user_id": request.user_id,
                "project": request.project,
                "prompt_snippet": request.prompt[:120],
                "classifier": classifier_output.model_dump(mode="json"),
                "allowed": decision.allowed,
                "final_tier": final_tier.value if final_tier else None,
                "reason": decision.reason,
                "cost_usd": cost_usd,
                "latency_ms": latency_ms,
                "budget": budget_state.model_dump(mode="json"),
            }
        )

        return RouteResponse(
            audit_id=audit_id,
            allowed=decision.allowed,
            final_tier=final_tier,
            model_name=model_name,
            output_text=output_text,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            reason=decision.reason,
            classifier=classifier_output,
            budget=budget_state,
        )

    def record_feedback(self, audit_id: int, signal: str) -> FeedbackResponse:
        row = self.audit.get(audit_id)
        if row is None:
            raise AuditNotFoundError(f"No audit record with id {audit_id}")
        if not row["allowed"]:
            raise InvalidFeedbackError(
                f"Audit record {audit_id} was a blocked request; tier feedback doesn't apply."
            )

        bucket = row["classifier"]["complexity"]
        self.feedback.record(audit_id, bucket, signal)
        return FeedbackResponse(ok=True, audit_id=audit_id, signal=signal)
