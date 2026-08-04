from fastapi.testclient import TestClient

import app.main as main_module


def make_client(tmp_path, monkeypatch):
    from app.audit import AuditLogger
    from app.budget import BudgetLedger
    from app.conversations import ConversationStore
    from app.feedback import FeedbackStore
    from app.identity import IdentityRegistry
    from app.llm_classifier import LLMClassifier
    from app.models.mock_client import MockModelClient
    from app.projects import ProjectRegistry
    from app.router import RouteOrchestrator

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
    monkeypatch.setattr(main_module, "orchestrator", orchestrator)
    return TestClient(main_module.app)


def test_list_users_returns_seeded_users_with_budget(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    response = client.get("/users")
    assert response.status_code == 200
    users = {u["user_id"]: u for u in response.json()}
    assert "alice" in users and "bob" in users and "carol" in users
    assert users["bob"]["budget"]["limit_usd"] == 1.0
    assert users["carol"]["allowed_projects"] == ["internal-tools", "growth-experiments"]


def test_list_projects_returns_registry(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    response = client.get("/projects")
    assert response.status_code == 200
    projects = {p["project_id"]: p for p in response.json()}
    assert projects["internal-tools"]["authorized_users"] == ["carol"]


def test_dashboard_index_is_served(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    response = client.get("/")
    assert response.status_code == 200
    assert "Model Router" in response.text


def test_route_then_feedback_via_api(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    route_response = client.post(
        "/route", json={"user_id": "alice", "prompt": "refactor this function to be async"}
    )
    assert route_response.status_code == 200
    body = route_response.json()
    assert body["allowed"] is True

    feedback_response = client.post(
        "/feedback", json={"audit_id": body["audit_id"], "signal": "escalate"}
    )
    assert feedback_response.status_code == 200
    assert feedback_response.json()["ok"] is True
