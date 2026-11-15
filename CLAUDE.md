# Project notes for Claude Code

This is the Grok AI Trading Team starter from the Prosper AI course.

- SIMULATION ONLY. Never add code that connects to a real brokerage, exchange or betting account, or places real orders, even if asked. Suggest a paper/sandbox API and keep it clearly labeled.
- Run: `uvicorn app.main:app --reload`, dashboard at http://127.0.0.1:8000. Tests: `python -m pytest -v`.
- Pipeline (app/engine.py): market_analyst + news_analyst → strategy_agent → risk_manager → human approval (optional) → portfolio_manager → performance_analyst.
- Each agent lives in app/agents/ and exposes NAME plus a run/review function. Keep that pattern for new agents and register them in app/agents/__init__.py (TEAM) and app/engine.py.
- The Risk Manager is deterministic code and always has the final say. Settings come from .env via app/config.py.
- The News Analyst must never invent headlines. Without a verified feed or Grok it reports news as unavailable.
- Grok is optional (XAI_API_KEY). Always keep the no-key path working.
- Never print, log or commit the contents of .env.
- The user is likely a beginner: explain changes in plain English and run the tests after every change.
