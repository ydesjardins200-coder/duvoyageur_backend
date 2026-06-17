"""
config.py
=========
All configuration comes from environment variables so the same code runs on
Railway, Supabase-backed, or locally. Set these in the Railway dashboard
(Variables tab). DATABASE_URL is the only thing that changes between using
Supabase Postgres vs Railway Postgres — paste whichever connection string.
"""
import os


class Settings:
    # --- database (Supabase OR Railway — just paste the connection string) ---
    # Local default is SQLite so you can run/test with zero setup.
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./dev.db")

    # --- Anthropic (the parser) ---
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    PARSE_MODEL: str = os.getenv("PARSE_MODEL", "claude-sonnet-4-6")

    # --- Facebook Messenger webhook ---
    FB_VERIFY_TOKEN: str = os.getenv("FB_VERIFY_TOKEN", "set-a-random-verify-token")
    FB_APP_SECRET: str = os.getenv("FB_APP_SECRET", "")   # used to verify payloads
    FB_PAGE_TOKEN: str = os.getenv("FB_PAGE_TOKEN", "")   # used later to send replies
    FB_GRAPH_VERSION: str = os.getenv("FB_GRAPH_VERSION", "v21.0")  # Send API version
    # Wait this many seconds before replying, so a burst of messages (e.g. text
    # then screenshot as separate events) yields ONE reply on the merged state.
    ACK_DEBOUNCE_SECONDS: float = float(os.getenv("ACK_DEBOUNCE_SECONDS", "5"))
    # Answer general questions (weather, season…) with a short concierge reply
    # instead of repeating the profiling line. Set to "0" to disable.
    CONCIERGE_ENABLED: bool = os.getenv("CONCIERGE_ENABLED", "1") not in ("0", "false", "False", "")

    # --- admin panel auth ---
    ADMIN_USER: str = os.getenv("ADMIN_USER", "admin")
    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "")  # MUST be set in prod

    # --- CORS: the Netlify domain(s) allowed to POST the intake form ---
    # Comma-separated, e.g. "https://duvoyageur.netlify.app,https://duvoyageur.ca"
    ALLOWED_ORIGINS: list[str] = os.getenv("ALLOWED_ORIGINS", "*").split(",")

    # Testing only: when truthy, enables POST /admin/reset to wipe all cases.
    # Leave UNSET in production so the wipe is fully disabled.
    ALLOW_RESET: str = os.getenv("ALLOW_RESET", "")


settings = Settings()
