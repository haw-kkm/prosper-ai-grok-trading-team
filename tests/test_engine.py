"""Tests for the trading engine and the six agents."""

import pytest

from app import config
from app.agents import news_analyst, risk_manager, strategy_agent
from app.engine import TradingFloor
from app.market import SimulatedMarket


def fresh_floor(seed=7):
    floor = TradingFloor(seed=seed)
    floor.broker.reset()
    floor.require_approval = False
    return floor


def test_full_simulation_runs_and_account_stays_consistent():
    floor = fresh_floor()
    for _ in range(300):
        floor.run_cycle()
    state = floor.state()
    assert state["mode"] == "SIMULATION"
    assert [a["name"] for a in state["agents"]] == [
        "Market Analyst", "News Analyst", "Strategy Agent",
        "Risk Manager", "Portfolio Manager", "Performance Analyst",
    ]
    assert state["cash"] >= 0
    assert len(state["positions"]) <= config.MAX_OPEN_POSITIONS
    assert floor.broker.trades, "expected some simulated trades in 300 cycles"


def test_prediction_market_prices_stay_between_0_and_1():
    m = SimulatedMarket(["PM-FED-CUT-DEC"], seed=1)
    for _ in range(2000):
        m.tick()
        assert 0.01 <= m.prices["PM-FED-CUT-DEC"] <= 0.99


def test_news_analyst_never_invents_news_without_grok():
    scores, note = news_analyst.run([{"symbol": "AAPL"}], use_grok=False)
    assert scores == {"AAPL": 0.0}
    assert "unavailable" in note


def test_risk_manager_pauses_after_daily_loss():
    ideas = [{"symbol": "AAPL", "action": "BUY", "price": 100.0, "confidence": 0.9, "reason": "x"}]
    verdicts, _ = risk_manager.review(ideas, {}, 90_000, 90_000, 100_000, 100_000)
    assert verdicts[0]["verdict"] == "REJECTED"


def test_risk_manager_caps_position_size():
    ideas = [{"symbol": "AAPL", "action": "BUY", "price": 100.0, "confidence": 0.9, "reason": "x"}]
    verdicts, _ = risk_manager.review(ideas, {}, 100_000, 100_000, 100_000, 100_000)
    assert verdicts[0]["verdict"] == "APPROVED"
    assert verdicts[0]["qty"] * 100.0 <= 100_000 * config.MAX_POSITION_PCT


def test_weak_setup_needs_review():
    ideas = [{"symbol": "AAPL", "action": "BUY", "price": 100.0, "confidence": 0.46, "reason": "x"}]
    verdicts, _ = risk_manager.review(ideas, {}, 100_000, 100_000, 100_000, 100_000)
    assert verdicts[0]["verdict"] == "NEEDS REVIEW"


def test_stop_loss_exit():
    exits = risk_manager.check_exits({"AAPL": {"qty": 10, "avg_price": 100.0}}, {"AAPL": 90.0})
    assert len(exits) == 1 and "Stop-loss" in exits[0][2]


def test_human_approval_queues_then_fills():
    floor = fresh_floor(seed=3)
    floor.require_approval = True
    for _ in range(300):
        floor.run_cycle()
        if floor.pending:
            break
    assert floor.pending, "expected a proposal waiting for approval"
    buys_before = sum(t["side"] == "BUY" for t in floor.broker.trades)
    pid = next(iter(floor.pending))
    ok, _ = floor.decide(pid, approve=True)
    assert ok
    assert sum(t["side"] == "BUY" for t in floor.broker.trades) == buys_before + 1


def test_dashboard_run_times_and_risk_status_match_risk_manager():
    floor = fresh_floor()
    assert all(a["last_run"] is None for a in floor.state()["agents"])  # never "ran" before the first cycle
    floor.run_cycle()
    state = floor.state()
    assert all(a["last_run"] for a in state["agents"])
    assert state["market_updated"] and state["reports"]

    floor.broker.cash -= floor.broker.starting_cash * 0.05  # simulate a 5% loss
    risk = floor.state()["risk_status"]
    assert risk["daily_loss_halt"] is True
    idea = [{"symbol": "AAPL", "action": "BUY", "price": 100.0, "confidence": 0.9, "reason": "x"}]
    verdicts, _ = risk_manager.review(idea, {}, risk["equity"], floor.broker.cash,
                                      floor.broker.day_start_equity, floor.peak_equity)
    assert verdicts[0]["verdict"] == "REJECTED"  # the dashboard flag agrees with the Risk Manager


def test_agent_error_is_recorded_for_the_dashboard(monkeypatch):
    floor = fresh_floor()

    def broken(*args, **kwargs):
        raise ValueError("bad input")

    monkeypatch.setattr(strategy_agent, "run", broken)
    with pytest.raises(ValueError):
        floor.run_cycle()
    err = floor.state()["last_error"]
    assert err["agent"] == "Strategy Agent" and "bad input" in err["error"]
