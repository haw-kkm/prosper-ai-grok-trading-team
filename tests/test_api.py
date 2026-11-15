"""Tests for the dashboard API (app/main.py)."""

from fastapi.testclient import TestClient

from app.main import app, floor


def test_dashboard_and_api():
    with TestClient(app) as client:
        floor.run_cycle()
        assert "Grok AI" in client.get("/").text
        assert client.get("/api/health").json()["mode"] == "SIMULATION"
        state = client.get("/api/state").json()
        assert state["mode"] == "SIMULATION"
        assert len(state["agents"]) == 6


def test_toggles_and_unknown_proposal():
    with TestClient(app) as client:
        before = client.get("/api/state").json()["require_approval"]
        assert client.post("/api/approval-mode").json()["require_approval"] != before
        client.post("/api/approval-mode")  # put it back
        assert client.post("/api/proposals/doesnotexist/approve").status_code == 409


def test_grok_endpoints_report_offline_demo_without_a_key():
    with TestClient(app) as client:
        assert client.post("/api/grok/test").status_code == 409
        assert client.post("/api/grok/research").status_code == 409
        status = client.get("/api/state").json()["grok_status"]
        assert status["state"] == "OFFLINE DEMO" and status["model"] is None and status["requests"] == 0
