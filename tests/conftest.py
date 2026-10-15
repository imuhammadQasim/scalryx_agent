import os
import tempfile
from pathlib import Path

# Settings and the engine are created at import, so configure them first.
_tmp = Path(tempfile.mkdtemp())
os.environ["AGENT_ENV_FILE"] = str(_tmp / "no.env")  # never read the real .env
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{(_tmp / 'test.db').as_posix()}"
os.environ["AGENT_API_KEY"] = "test-key"
os.environ["ADMIN_TOKEN"] = "admin-token"
os.environ["OPENAI_API_KEY"] = "sk-test"
os.environ["OPENAI_MODEL"] = "gpt-5.4-mini"
os.environ["OPENAI_REASONING_EFFORT"] = "low"
for key in ("CAL_API_KEY", "CAL_EVENT_TYPE_ID", "SMTP_HOST", "OWNER_EMAIL"):
    os.environ.pop(key, None)
