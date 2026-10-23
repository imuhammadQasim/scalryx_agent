"""Settings, read from the environment or a .env file next to the app."""

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # Blank lines copied from .env.example mean "not set", not "empty value".
    # AGENT_ENV_FILE points elsewhere (tests use it to stay off the real .env).
    model_config = SettingsConfigDict(
        env_file=os.environ.get("AGENT_ENV_FILE", BASE_DIR / ".env"), extra="ignore", env_ignore_empty=True
    )

    # OpenAI. Passed to the SDK explicitly: a key only in .env never reaches
    # os.environ, where the SDK would otherwise look.
    openai_api_key: str = ""
    openai_model: str = "gpt-5.4-mini"
    # Reasoning effort for reasoning models (none, low, medium, high). Low
    # keeps first words under two seconds. Leave empty for models without
    # reasoning, such as gpt-4.1-mini.
    openai_reasoning_effort: str = "low"
    max_steps_per_turn: int = 6

    # Shared secret the Next.js proxy sends as X-Agent-Key. Empty disables the
    # check, which is only acceptable on localhost.
    agent_api_key: str = ""
    # Bearer token for /v1/admin/* endpoints. Empty disables them.
    admin_token: str = ""

    # --- Lead dashboard (/api/*) ---
    # The one admin account, written to the database by `python -m app.crm.seed`.
    admin_email: str = ""
    admin_password: str = ""
    # Shared secret for POST /api/leads/ingest (X-Ingest-Key). Empty disables it.
    ingest_key: str = ""
    ingest_rate_per_minute: int = 30
    # Origins allowed to call /api with the session cookie, comma separated.
    dashboard_origins: str = "http://localhost:5173"
    # Send the session cookie over HTTPS only. Switch off for plain-http local dev.
    cookie_secure: bool = True
    session_days: int = 7
    # Behind a reverse proxy (Caddy), take the client IP from X-Forwarded-For.
    trust_proxy: bool = False
    login_max_failures: int = 5
    login_lockout_minutes: int = 15

    database_url: str = f"sqlite+aiosqlite:///{(BASE_DIR / 'data' / 'agent.db').as_posix()}"
    knowledge_path: Path = BASE_DIR / "knowledge" / "scalryx.json"

    # Cal.com: an API key from Settings > Developer > API keys, and the id of
    # the "Free growth call" event type. Without them the agent shares
    # cal_booking_url (or the contact page) instead of booking in the chat.
    cal_api_key: str = ""
    cal_event_type_id: int | None = None
    cal_api_url: str = "https://api.cal.com/v2"
    cal_booking_url: str = ""

    # SMTP for lead alerts, form confirmations and project briefs. Gmail by
    # default: SMTP_USER and SMTP_PASSWORD (a Gmail App Password) are all it
    # needs. Any other server (OCI Email Delivery, Zoho, ...) works the same
    # way with SMTP_HOST set. Without credentials, emails are only logged.
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    # Where team alerts go. Comma separated for several inboxes, e.g.
    # "ahad@scalryx.com,muhammad.qasim.dev07@gmail.com".
    owner_email: str = ""
    # Times in the team's alerts are shown in this zone.
    owner_timezone: str = "America/New_York"

    # Most clients are in the US: when a visitor's own time zone is unknown,
    # times are quoted in this one. A known browser time zone always wins.
    default_timezone: str = "America/New_York"

    # Abuse and cost brakes.
    rate_limit_per_minute: int = 12
    max_user_turns: int = 40
    max_message_chars: int = 1000

    @property
    def dashboard_origin_list(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.dashboard_origins.split(",") if o.strip()]

    @property
    def calendar_enabled(self) -> bool:
        return bool(self.cal_api_key and self.cal_event_type_id)

    @property
    def mail_from(self) -> str:
        """The sender. Gmail only sends as the signed-in account, so it
        defaults to that."""
        return self.smtp_from or (f"Scalryx <{self.smtp_user}>" if self.smtp_user else "")

    @property
    def team_emails(self) -> list[str]:
        """Every inbox that gets alerts: OWNER_EMAIL, or else the SMTP account itself."""
        listed = [e.strip() for e in (self.owner_email or self.smtp_user).split(",")]
        return [e for e in listed if e]

    @property
    def team_email(self) -> str:
        """The first alert inbox, used as the Reply-To on mail to visitors."""
        return next(iter(self.team_emails), "")

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host and self.mail_from)


@lru_cache
def get_settings() -> Settings:
    return Settings()
