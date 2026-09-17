import os

GEMINI_API_KEY = (os.environ.get("GEMINI_API_KEY") or "").strip()
MODEL = "gemini-flash-lite-latest"

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
OWNER_USER_ID = os.environ.get("OWNER_USER_ID", "Uefe8d562986ba54c6f232e68f5a1ee45")

DB_PATH = os.path.join(os.path.dirname(__file__), "records.db")
