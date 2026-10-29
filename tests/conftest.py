"""Test setup: tests never touch real services or the real .env secrets."""
import os
import sys
from pathlib import Path

for key in ("SUPABASE_URL", "SUPABASE_KEY", "TELEGRAM_BOT_TOKEN", "GROQ_API_KEY", "DISABLED_SOURCES"):
    os.environ[key] = ""
os.environ["PYTHONUTF8"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
