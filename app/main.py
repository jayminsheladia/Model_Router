from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from app.projects import Project
from app.router import AuditNotFoundError, InvalidFeedbackError, RouteOrchestrator, UnknownUserError
from app.schemas import (
    BudgetState,
    FeedbackRequest,
    FeedbackResponse,
    RouteRequest,
    RouteResponse,
    UserSummary,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

load_dotenv()

app = FastAPI(title="Policy-Aware Model Router")
orchestrator = RouteOrchestrator()


@app.middleware("http")
async def no_cache(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


@app.post("/route", response_model=RouteResponse)
def route(request: RouteRequest) -> RouteResponse:
    try:
        return orchestrator.route(request)
    except UnknownUserError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/feedback", response_model=FeedbackResponse)
def feedback(request: FeedbackRequest) -> FeedbackResponse:
    try:
        return orchestrator.record_feedback(request.audit_id, request.signal)
    except AuditNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidFeedbackError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/audit")
def get_audit(n: int = 20) -> list[dict]:
    return orchestrator.audit.read_recent(n)


@app.get("/users", response_model=list[UserSummary])
def list_users() -> list[UserSummary]:
    return [
        UserSummary(
            user_id=user.user_id,
            team=user.team,
            max_tier=user.max_tier,
            allowed_projects=user.allowed_projects,
            project_tier_overrides=user.project_tier_overrides,
            budget=orchestrator.budget.check(user),
        )
        for user in orchestrator.identity.list_users()
    ]


@app.get("/projects", response_model=list[Project])
def list_projects() -> list[Project]:
    return orchestrator.project_registry.list_projects()


@app.get("/users/{user_id}/budget", response_model=BudgetState)
def get_budget(user_id: str) -> BudgetState:
    user = orchestrator.identity.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail=f"Unknown user: {user_id}")
    return orchestrator.budget.check(user)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="dashboard")
