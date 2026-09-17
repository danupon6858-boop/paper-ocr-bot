import os

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AQ.Ab8RN6Imw_2bc5XVUxA54u7hZMvmaDj8zTyHVEwo07SLk2u1nA")
MODEL = "gemini-3.6-flash"

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "185987717ade027b657d31694033db85")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "vaJnOlD0NYATki2QwmbnWV4yXJNqzRHfWk8vZxhEYx6tvxS99mievWGfepC5ZmmposcIPaazxpMBIkYDsffs2WvDwKR8emUbL43LCgks+SA4y67KbM2YY7o1A2Ttu6AJdIp/ECtRn1hj6WgKrooAdgdB04t89/1O/w1cDnyilFU=")
OWNER_USER_ID = "Uefe8d562986ba54c6f232e68f5a1ee45"

DB_PATH = os.path.join(os.path.dirname(__file__), "records.db")
