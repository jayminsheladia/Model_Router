from __future__ import annotations

import uuid

from app import classifier, policy
from app.audit import AuditLogger
from app.budget import BudgetLedger
from app.conversations import ConversationAccessError, ConversationStore
from app.feedback import FeedbackStore
from app.identity import IdentityRegistry
from app.llm_classifier import LLMClassifier
from app.models.groq_client import GroqModelClient
from app.models.mock_client import ModelUnavailableError, MockModelClient
from app.projects import ProjectRegistry
from app.schemas import (
    TIER_ORDER,
    FeedbackResponse,
    PolicyDecision,
    RouteRequest,
    RouteResponse,
    Tier,
)


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
        model_client: MockModelClient | GroqModelClient | None = None,
        audit: AuditLogger | None = None,
        feedback: FeedbackStore | None = None,
        project_registry: ProjectRegistry | None = None,
        llm_classifier: LLMClassifier | None = None,
        conversations: ConversationStore | None = None,
    ):
        self.identity = identity or IdentityRegistry()
        self.budget = budget or BudgetLedger()
        if model_client is not None:
            self.model_client = model_client
        else:
            groq_client = GroqModelClient()
            self.model_client = groq_client if groq_client.enabled else MockModelClient()
        self.audit = audit or AuditLogger()
        self.feedback = feedback or FeedbackStore()
        self.project_registry = project_registry or ProjectRegistry()
        self.llm_classifier = llm_classifier or LLMClassifier()
        self.conversations = conversations or ConversationStore()

    def route(self, request: RouteRequest) -> RouteResponse:
        user = self.identity.get_user(request.user_id)
        if user is None:
            raise UnknownUserError(f"Unknown user: {request.user_id}")

        history: list[dict] = []
        conversation_access_error: str | None = None
        if request.conversation_id:
            try:
                history = self.conversations.get_history(request.conversation_id, request.user_id)
            except ConversationAccessError:
                conversation_access_error = (
                    f"Blocked: conversation '{request.conversation_id}' does not belong to "
                    f"{request.user_id}."
                )

        classifier_output = classifier.classify(request.prompt, bias_lookup=self.feedback.get_bias)
        classifier_output = self.llm_classifier.reclassify_if_ambiguous(
            request.prompt, classifier_output
        )
        budget_state = self.budget.check(user)

        if conversation_access_error is not None:
            decision = PolicyDecision(allowed=False, final_tier=None, reason=conversation_access_error)
        else:
            decision = policy.decide(
                user,
                request.project,
                classifier_output,
                budget_state,
                self.project_registry,
                request.routing_mode,
            )

        reason = decision.reason
        conversation_id: str | None = None

        if not decision.allowed:
            final_tier = None
            model_name = None
            output_text = None
            cost_usd = 0.0
            latency_ms = 0.0
        else:
            max_cost = self.model_client.estimate_max_cost(
                decision.final_tier, request.prompt, history
            )
            reservation_id = self.budget.reserve(user, max_cost)
            if reservation_id is None:
                # Another in-flight request consumed the remaining budget between
                # the check above and here.
                decision = decision.model_copy(
                    update={
                        "allowed": False,
                        "reason": (
                            f"Blocked: {request.user_id} has exhausted their monthly budget "
                            f"(concurrent request consumed the remainder)."
                        ),
                    }
                )
                reason = decision.reason
                final_tier = None
                model_name = None
                output_text = None
                cost_usd = 0.0
                latency_ms = 0.0
                budget_state = self.budget.check(user)
            else:
                (
                    final_tier,
                    output_text,
                    cost_usd,
                    latency_ms,
                    model_name,
                    failover_note,
                ) = self._call_with_failover(decision.final_tier, request.prompt, history)
                reason += failover_note
                if final_tier is None:
                    self.budget.release(reservation_id)
                    decision = decision.model_copy(update={"allowed": False})
                else:
                    self.budget.settle(reservation_id, cost_usd)
                    budget_state = self.budget.check(user)
                    conversation_id = request.conversation_id or str(uuid.uuid4())
                    self.conversations.append(
                        conversation_id, request.user_id, "user", request.prompt
                    )
                    self.conversations.append(
                        conversation_id, request.user_id, "assistant", output_text
                    )

        audit_id = self.audit.record(
            {
                "user_id": request.user_id,
                "project": request.project,
                "prompt_snippet": request.prompt[:120],
                "classifier": classifier_output.model_dump(mode="json"),
                "allowed": decision.allowed,
                "final_tier": final_tier.value if final_tier else None,
                "reason": reason,
                "cost_usd": cost_usd,
                "latency_ms": latency_ms,
                "budget": budget_state.model_dump(mode="json"),
                "conversation_id": conversation_id,
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
            reason=reason,
            classifier=classifier_output,
            budget=budget_state,
            conversation_id=conversation_id,
        )

    def _call_with_failover(
        self, starting_tier: Tier, prompt: str, history: list[dict] | None = None
    ) -> tuple[Tier | None, str | None, float, float, str | None, str]:
        tier_idx = TIER_ORDER.index(starting_tier)
        tiers_to_try = list(reversed(TIER_ORDER[: tier_idx + 1]))  # starting_tier, then cheaper ones

        failed_tiers: list[Tier] = []
        for tier in tiers_to_try:
            try:
                output_text, _tokens, cost_usd, latency_ms, model_name = self.model_client.call(
                    tier, prompt, history
                )
            except ModelUnavailableError:
                failed_tiers.append(tier)
                continue
            note = ""
            if failed_tiers:
                failed_names = ", ".join(t.value for t in failed_tiers)
                note = f" Failover: {failed_names} unavailable, served at {tier.value}."
            return tier, output_text, cost_usd, latency_ms, model_name, note

        failed_names = ", ".join(t.value for t in failed_tiers)
        note = f" Blocked: all eligible tiers unavailable ({failed_names})."
        return None, None, 0.0, 0.0, None, note

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
