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

    # --- session cookie (the form login) ---
    # SECRET_KEY signs the session cookie. MUST be set to a long random string
    # in prod and kept STABLE (changing it logs everyone out). Generate one:
    #   python -c "import secrets; print(secrets.token_urlsafe(48))"
    SECRET_KEY: str = os.getenv("SECRET_KEY", "dev-only-insecure-change-me")
    # How long a login lasts, in seconds (default 14 days).
    SESSION_MAX_AGE: int = int(os.getenv("SESSION_MAX_AGE", str(60 * 60 * 24 * 14)))
    # Send the cookie only over HTTPS. Set to 1 in prod (Railway is HTTPS);
    # leave unset locally so http://localhost keeps you logged in.
    SECURE_COOKIES: bool = os.getenv("SECURE_COOKIES", "") not in ("", "0", "false", "False")

    # --- CORS: the Netlify domain(s) allowed to POST the intake form ---
    # Comma-separated, e.g. "https://duvoyageur.netlify.app,https://duvoyageur.ca"
    ALLOWED_ORIGINS: list[str] = os.getenv("ALLOWED_ORIGINS", "*").split(",")

    # --- Cloudflare R2 (screenshot object storage; S3-compatible) ---
    # When set, screenshots are uploaded to R2 instead of stored as base64 in
    # the DB. Leave unset to keep the inline-base64 fallback (local/tests).
    R2_ACCOUNT_ID: str = os.getenv("R2_ACCOUNT_ID", "")
    R2_ACCESS_KEY_ID: str = os.getenv("R2_ACCESS_KEY_ID", "")
    R2_SECRET_ACCESS_KEY: str = os.getenv("R2_SECRET_ACCESS_KEY", "")
    R2_BUCKET: str = os.getenv("R2_BUCKET", "")
    # Optional explicit endpoint; otherwise derived from the account id.
    R2_ENDPOINT: str = os.getenv("R2_ENDPOINT", "")

    # Testing only: when truthy, enables POST /admin/reset to wipe all cases.
    # Leave UNSET in production so the wipe is fully disabled.
    ALLOW_RESET: str = os.getenv("ALLOW_RESET", "")


settings = Settings()
