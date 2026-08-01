from typing import Optional

from app.identity import User
from app.projects import ProjectRegistry
from app.schemas import TIER_ORDER, BudgetState, ClassifierOutput, PolicyDecision


def decide(
    user: User,
    project: Optional[str],
    classifier_output: ClassifierOutput,
    budget_state: BudgetState,
    project_registry: ProjectRegistry,
    routing_mode: str = "balanced",
) -> PolicyDecision:
    if classifier_output.off_policy:
        return PolicyDecision(
            allowed=False,
            final_tier=None,
            reason="Blocked: prompt matched off-policy (non-work) usage pattern.",
        )

    if budget_state.over_hard_limit:
        return PolicyDecision(
            allowed=False,
            final_tier=None,
            reason=(
                f"Blocked: {user.user_id} has exhausted their monthly budget "
                f"(${budget_state.spent_usd:.2f} / ${budget_state.limit_usd:.2f})."
            ),
        )

    if user.allowed_projects:
        if project is None:
            return PolicyDecision(
                allowed=False,
                final_tier=None,
                reason=f"Blocked: {user.user_id} is restricted to specific projects; no project was given.",
            )
        if project not in user.allowed_projects:
            return PolicyDecision(
                allowed=False,
                final_tier=None,
                reason=f"Blocked: {user.user_id} is not authorized for project '{project}'.",
            )

    if project is not None:
        registered = project_registry.get_project(project)
        if registered is not None and user.user_id not in registered.authorized_users:
            return PolicyDecision(
                allowed=False,
                final_tier=None,
                reason=(
                    f"Blocked: {user.user_id} is not authorized for project '{project}' "
                    f"(project-level restriction)."
                ),
            )

    tier = classifier_output.suggested_tier

    if routing_mode == "cost":
        tier_idx = TIER_ORDER.index(tier)
        if tier_idx > 0:
            tier = TIER_ORDER[tier_idx - 1]
        cost_mode_reason = " Cost mode: pre-emptively downgraded one tier."
    else:
        cost_mode_reason = ""

    max_tier = user.project_tier_overrides.get(project, user.max_tier) if project else user.max_tier
    max_tier_idx = TIER_ORDER.index(max_tier)
    if TIER_ORDER.index(tier) > max_tier_idx:
        tier = max_tier
        capped_reason = f" Capped to max allowed tier for {user.user_id}"
        capped_reason += f" on project '{project}'" if project in user.project_tier_overrides else ""
        capped_reason += f" ({max_tier})."
    else:
        capped_reason = ""

    if budget_state.over_soft_limit and routing_mode != "quality":
        tier_idx = TIER_ORDER.index(tier)
        if tier_idx > 0:
            tier = TIER_ORDER[tier_idx - 1]
        downgrade_reason = (
            f" Downgraded one tier: over soft budget limit "
            f"(${budget_state.spent_usd:.2f} / ${budget_state.limit_usd:.2f})."
        )
    elif budget_state.over_soft_limit and routing_mode == "quality":
        downgrade_reason = " Quality mode: ignoring soft-budget downgrade."
    else:
        downgrade_reason = ""

    reason = (
        f"Allowed at {tier} tier ({classifier_output.reasoning})"
        + cost_mode_reason
        + capped_reason
        + downgrade_reason
    )

    return PolicyDecision(allowed=True, final_tier=tier, reason=reason)
