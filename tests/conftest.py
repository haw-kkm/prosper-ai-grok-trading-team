import os
import pathlib
import sys
import tempfile

# Tests never call Grok or Alpha Vantage with your keys and never touch your real data files.
os.environ["XAI_API_KEY"] = ""
os.environ["ALPHAVANTAGE_API_KEY"] = ""
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import broker, decisions, engine, stockdata  # noqa: E402
from app.agents import news_analyst  # noqa: E402

_tmp = pathlib.Path(tempfile.mkdtemp())
broker.STATE_FILE = _tmp / "test_account.json"
news_analyst.NEWS_FILE = _tmp / "news.json"          # never read your real news file
engine.RESEARCH_LOG = _tmp / "grok_research.jsonl"   # never write to your real research log
stockdata.DB_PATH = _tmp / "market_data.db"          # never touch your real market data DB
decisions.DB_PATH = _tmp / "decisions.db"            # never touch your real decision ledger
