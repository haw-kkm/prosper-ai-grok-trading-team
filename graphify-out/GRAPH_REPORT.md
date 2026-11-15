# Graph Report - prosper-ai-grok-trading-team  (2026-11-16)

## Corpus Check
- 43 files · ~62,393 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 2 file(s) not represented in the graph (top: .example 1, (none) 1)

## Summary
- 873 nodes · 2458 edges · 31 communities (27 shown, 4 thin omitted)
- Extraction: 96% EXTRACTED · 4% INFERRED · 0% AMBIGUOUS · INFERRED: 88 edges (avg confidence: 0.88)
- Token cost: 294,611 input · 0 output

## Community Hubs (Navigation)
- Risk & Decision Tests
- Decision Cycle Core
- Dashboard UI Rendering
- Indicators & Features
- Backtest Tests
- Kraken Crypto Data Tests
- Alpha Vantage Stock Tests
- FastAPI Endpoints
- Grok & Agent Engine
- Crypto Data Import
- Backtest Engine
- Stock Data Provider
- US Market Calendar
- Decision & Approval Docs
- Trading Floor Orchestrator
- Engine Tests
- Grok Health & Guard
- Risk Management Docs
- Paper Broker
- Six-Agent Registry
- Backtesting Lab Docs
- Final Audit Findings
- Crypto Data Docs
- SQL Bar Source
- Stock Data Docs
- Grok Research Interface
- Simulated Market
- Joined Archive Stream
- Project Rules & Pipeline
- Grok Optional Research Docs

## God Nodes (most connected - your core abstractions)
1. `view()` - 44 edges
2. `Market` - 44 edges
3. `TradingFloor` - 32 edges
4. `fetch_kraken()` - 26 edges
5. `state()` - 26 edges
6. `run()` - 24 edges
7. `market_type()` - 24 edges
8. `Final Integrated Project Audit Report (Lesson 13)` - 24 edges
9. `seed()` - 21 edges
10. `run_backtest()` - 20 edges

## Surprising Connections (you probably didn't know these)
- `Old Simulator Auto-Fills Without Approval` --references--> `Offline Demo Market Simulator (app/market.py)`  [INFERRED]
  results/FINAL_PROJECT_AUDIT.md → README.md
- `test_1_2_3_real_loader_split_and_gap_detection()` --calls--> `run_backtest()`  [EXTRACTED]
  tests/test_backtest.py → app/backtest.py
- `test_insufficient_data_and_unsupported_prediction_markets()` --calls--> `run_backtest()`  [EXTRACTED]
  tests/test_backtest.py → app/backtest.py
- `index()` --references--> `get()`  [EXTRACTED]
  app/main.py → tests/test_decisions.py
- `Performance Analyst Agent` --conceptually_related_to--> `Buy & Hold Benchmark`  [INFERRED]
  README.md → docs/LESSON11_PROGRESS.md

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Per-view lazy data loaders (fetched on view switch / throttled refresh)** — dashboard_index_loadstocks, dashboard_index_loadcrypto, dashboard_index_loaddecisions, dashboard_index_loadbacktest, dashboard_index_showview, dashboard_index_refresh [EXTRACTED 1.00]
- **Backtest run submit-poll-render flow** — dashboard_index_btrun, dashboard_index_btwatch, dashboard_index_btdone, dashboard_index_loadbacktest, dashboard_index_renderbacktest, dashboard_index_api_backtest [EXTRACTED 1.00]
- **Decision Center + Risk Control Center on one /api/decisions payload** — dashboard_index_loaddecisions, dashboard_index_decisioncall, dashboard_index_renderdecview, dashboard_index_renderdecisions, dashboard_index_renderriskctl, dashboard_index_api_decisions, dashboard_index_paper_decision_ledger [EXTRACTED 1.00]
- **Six-Agent Pipeline With Human Approval Gate** — readme_market_analyst, readme_news_analyst, readme_strategy_agent, readme_risk_manager, readme_human_approval, readme_portfolio_manager, readme_performance_analyst [EXTRACTED 1.00]
- **Lesson 10 Deterministic Risk Controls** — readme_daily_loss_limit, readme_drawdown_kill_switch, readme_losing_streak_cooldown, readme_simulated_stop_loss, docs_lesson10_progress_strictest_limit_wins, docs_lesson10_progress_per_trade_risk [EXTRACTED 1.00]
- **Label Missing Data, Never Fabricate (Fail Closed)** — claude_news_no_invented_headlines, docs_lesson9_progress_news_unavailable, docs_lesson7_progress_gap_classification, docs_lesson10_progress_fail_closed [INFERRED 0.85]

## Communities (31 total, 4 thin omitted)

### Community 0 - "Risk & Decision Tests"
Cohesion: 0.09
Nodes (68): approve(), db_rows(), fills(), get(), Market, run(), seed(), test_approval_and_trade_state_survive_a_server_restart() (+60 more)

### Community 1 - "Decision Cycle Core"
Cohesion: 0.07
Nodes (46): _account(), approve(), buy_limits(), candles_since(), check_stops(), _create(), _daily(), _db() (+38 more)

### Community 2 - "Dashboard UI Rendering"
Cohesion: 0.06
Nodes (58): dashboard/index.html (Grok AI Trading Command Center SPA), activeError(), agentState(), agentWarnings(), /api/backtest endpoints (options/runs/run), /api/crypto endpoints, /api/decisions endpoints (run/approve/reject), /api/grok/{test,research} endpoints (+50 more)

### Community 3 - "Indicators & Features"
Cohesion: 0.06
Nodes (38): analyze_bars(), _indicators(), _pct(), connect(), iso(), _align_higher(), build(), _chunks() (+30 more)

### Community 4 - "Backtest Tests"
Cohesion: 0.05
Nodes (32): run(), run(), run(), _Book, _day(), _Higher, _iso(), ListSource (+24 more)

### Community 5 - "Kraken Crypto Data Tests"
Cohesion: 0.06
Nodes (38): market_report(), detail(), _display(), fetch_kraken(), gap_report(), latest_open(), load_candles(), pair_map() (+30 more)

### Community 6 - "Alpha Vantage Stock Tests"
Cohesion: 0.08
Nodes (32): detail(), fetch_alpha_vantage(), skip(), import_csv(), _ingest(), latest_date(), load_bars(), _log() (+24 more)

### Community 7 - "FastAPI Endpoints"
Cohesion: 0.06
Nodes (22): Approval, approve(), backtest_result(), backtest_run(), backtest_runs(), crypto(), crypto_pair(), decisions_approve() (+14 more)

### Community 8 - "Grok & Agent Engine"
Cohesion: 0.10
Nodes (9): run(), ask_grok_json(), _error_detail(), _record(), redact(), _request(), test_news_analyst_never_invents_news_without_grok(), test_news_analyst_never_asks_grok_without_provided_news() (+1 more)

### Community 9 - "Crypto Data Import"
Cohesion: 0.11
Nodes (15): archive_members(), import_archive(), import_csv(), _ingest(), last_closed_open(), _log(), main(), open_archive() (+7 more)

### Community 10 - "Backtest Engine"
Cohesion: 0.14
Nodes (15): _bounds(), _candle(), code_version(), demo_source(), _finish(), get_run(), list_runs(), _read() (+7 more)

### Community 11 - "Stock Data Provider"
Cohesion: 0.10
Nodes (10): main(), options(), _get(), backtest_options(), connect(), main(), overview(), parse_alpha_vantage() (+2 more)

### Community 12 - "US Market Calendar"
Cohesion: 0.19
Nodes (14): quality(), bar_is_final(), _easter(), holidays(), is_trading_day(), last_completed_session(), _last_weekday(), _nth_weekday() (+6 more)

### Community 13 - "Decision & Approval Docs"
Cohesion: 0.18
Nodes (17): Approval Blocked on Cash/Position Change (_snapshot), Isolated SYNTHETIC DEMO Session, Human Approval Skips Risk Re-check Bug, Approval With Full Re-check (approve), Decision Cycle (decisions.run), Lesson 9 Progress: AI Trading Decision Engine, NEWS_UNAVAILABLE Status, Offline Verification Script (verify_l9.py) (+9 more)

### Community 14 - "Trading Floor Orchestrator"
Cohesion: 0.22
Nodes (7): load_news(), TradingFloor, test_grok_buttons_are_rate_limited(), test_grok_research_cannot_trade_approve_or_change_limits(), test_load_news_skips_invalid_items(), test_offline_demo_without_key_makes_no_request(), test_stale_grok_news_scores_stop_feeding_the_strategy()

### Community 15 - "Engine Tests"
Cohesion: 0.17
Nodes (12): check_exits(), _exposure(), review(), fresh_floor(), test_agent_error_is_recorded_for_the_dashboard(), test_dashboard_run_times_and_risk_status_match_risk_manager(), test_full_simulation_runs_and_account_stays_consistent(), test_human_approval_queues_then_fills() (+4 more)

### Community 16 - "Grok Health & Guard"
Cohesion: 0.19
Nodes (6): grok_enabled(), status(), test_connection(), health(), http_error(), test_connection_states_and_the_key_never_leaks()

### Community 17 - "Risk Management Docs"
Cohesion: 0.25
Nodes (13): Additive DB Migration (NEW_COLUMNS), Lesson 10 Progress: Risk Management System, NaN Price Crash Bug (fixed), Per-Trade Risk 1% (incl. fees + slippage), risk_state / risk_events / risk_settings Tables, Strictest-Limit-Wins Position Sizing (_limits), Daily-Loss Baseline Never Resets Bug, Daily Loss Limit (-3%) (+5 more)

### Community 18 - "Paper Broker"
Cohesion: 0.21
Nodes (3): PaperBroker, lifespan(), reset()

### Community 20 - "Backtesting Lab Docs"
Cohesion: 0.29
Nodes (11): Backtest Engine (app/backtest.py), Buy & Hold Benchmark, First Real Backtest Run 34f54f91bb2a (BTC/USD 1h), False INTERRUPTED Status Bug (fixed), Lesson 11 Progress: Multi-Market Backtesting Lab, Shared Risk Helpers (buy_limits, stop_exit), Backtest Approval Policy (PROPOSAL_ONLY / SIMULATED_APPROVAL), BACKTEST_COSTS Assumptions (+3 more)

### Community 21 - "Final Audit Findings"
Cohesion: 0.29
Nodes (12): Lesson 13 Progress: Final Integrated Audit, Grok HTTP 403 Labeled auth Instead of billing, 70/30 Time-Based Train/Test Split, httpx 0.28.1, pytest 8.3.4, Backtest Reproducibility Check, Final Integrated Project Audit Report (Lesson 13), Lesson 12 Session Runner Missing (+4 more)

### Community 22 - "Crypto Data Docs"
Cohesion: 0.27
Nodes (11): OHLCV Validation, crypto_candles / crypto_fetch_log / crypto_rejected Tables, Kraken 720-Candle Cap, Kraken AssetPairs Pair Mapping (7-day cache), Lesson 7 Progress: Real Crypto Data (Kraken Spot), BTC/USD vs XXBTZUSD OHLC Key Bug, Crypto (Real) Tab (Lesson 7), Kraken Public Spot OHLC API (+3 more)

### Community 24 - "Stock Data Docs"
Cohesion: 0.27
Nodes (10): Alpha Vantage 25-Requests/Day Guard, Lesson 6 Progress: Real US Stock Data, RAW Prices Not Split-Adjusted, US Market Calendar (NYSE Holidays), Alpha Vantage TIME_SERIES_DAILY (free tier), Prosper AI: Grok AI Trading Team, Prediction Markets (Simulated PM-*), Stocks (Real) Tab (Lesson 6) (+2 more)

### Community 25 - "Grok Research Interface"
Cohesion: 0.25
Nodes (5): research(), research_real(), sample_reports(), test_failures_are_errors_never_success_and_never_retried(), test_market_research_records_inputs_and_drops_invalid_answers()

### Community 26 - "Simulated Market"
Cohesion: 0.31
Nodes (3): get_state(), SimulatedMarket, test_prediction_market_prices_stay_between_0_and_1()

### Community 28 - "Project Rules & Pipeline"
Cohesion: 0.25
Nodes (6): Claude Code Project Notes, Six-Agent Trading Pipeline, Black-and-Gold Trading Dashboard, fastapi 0.115.6, uvicorn 0.32.1, Agents Are Fixed Python Rules (No Learning)

### Community 29 - "Grok Optional Research Docs"
Cohesion: 0.33
Nodes (6): redact() Masks xAI Team IDs, research_stocks Grok Interface (not wired), market_report() Market Analyst Report, Offline Demo Market Simulator (app/market.py), Grok (xAI) Optional Research, Market Analyst Agent

## Knowledge Gaps
- **4 isolated node(s):** `/api/state endpoint`, `Simulator control endpoints (/api/toggle, /api/approval-mode, /api/reset, /api/proposals)`, `Backtest Approval Policy (PROPOSAL_ONLY / SIMULATED_APPROVAL)`, `Lesson 12 Session Runner Missing`
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 231 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **4 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `TradingFloor` connect `Trading Floor Orchestrator` to `Risk & Decision Tests`, `Kraken Crypto Data Tests`, `Alpha Vantage Stock Tests`, `FastAPI Endpoints`, `Grok & Agent Engine`, `Engine Tests`, `Grok Health & Guard`, `Paper Broker`, `Simulated Market`?**
  _High betweenness centrality (0.047) - this node is a cross-community bridge._
- **Are the 2 inferred relationships involving `TradingFloor` (e.g. with `PaperBroker` and `SimulatedMarket`) actually correct?**
  _`TradingFloor` has 2 INFERRED edges - model-reasoned connections that need verification._
- **What connects `/api/state endpoint`, `Simulator control endpoints (/api/toggle, /api/approval-mode, /api/reset, /api/proposals)`, `Backtest Approval Policy (PROPOSAL_ONLY / SIMULATED_APPROVAL)` to the rest of the system?**
  _4 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Risk & Decision Tests` be split into smaller, more focused modules?**
  _Cohesion score 0.08508891928864569 - nodes in this community are weakly interconnected._
- **Why does `renderCrypto()` connect `Dashboard UI Rendering` to `Crypto Data Import`?**
  _High betweenness centrality (0.020) - this node is a cross-community bridge._
- **Should `Decision Cycle Core` be split into smaller, more focused modules?**
  _Cohesion score 0.06937799043062201 - nodes in this community are weakly interconnected._
- **Why does `renderRisk()` connect `Dashboard UI Rendering` to `Grok & Agent Engine`?**
  _High betweenness centrality (0.018) - this node is a cross-community bridge._