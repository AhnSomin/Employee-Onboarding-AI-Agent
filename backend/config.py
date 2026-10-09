import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
DB_PATH = Path(os.getenv("ONBOARDING_DB", ROOT / "data" / "state.db"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
LAW_API_OC = os.getenv("LAW_API_OC", "")


def today() -> date:
    v = os.getenv("DEMO_TODAY")
    return date.fromisoformat(v) if v else date.today()
