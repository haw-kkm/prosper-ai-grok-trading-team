"""The six agents, in the order they run each cycle."""

from . import (  # noqa: F401
    market_analyst,
    news_analyst,
    strategy_agent,
    risk_manager,
    portfolio_manager,
    performance_analyst,
)

TEAM = [
    market_analyst,
    news_analyst,
    strategy_agent,
    risk_manager,
    portfolio_manager,
    performance_analyst,
]
