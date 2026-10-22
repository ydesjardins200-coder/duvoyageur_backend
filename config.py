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

    # --- Anthropic (the parser + concierge) ---
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    # Primary model: Haiku 4.5 is ~5x cheaper than Sonnet and plenty for
    # structured extraction / short concierge replies.
    PARSE_MODEL: str = os.getenv("PARSE_MODEL", "claude-haiku-4-5-20251001")
    # When Haiku reports low confidence on a parse, retry once on a stronger
    # model (with a higher-res image). Set PARSE_FALLBACK_MODEL="" to disable.
    PARSE_FALLBACK_MODEL: str = os.getenv("PARSE_FALLBACK_MODEL", "claude-sonnet-4-6")
    PARSE_FALLBACK_CONF: float = float(os.getenv("PARSE_FALLBACK_CONF", "0.6"))
    # Screenshots are downscaled before being sent to the API to save vision
    # tokens. Primary uses a smaller edge; the fallback uses a larger one so the
    # stronger model gets maximum detail on the hard cases.
    PARSE_IMG_MAX_EDGE: int = int(os.getenv("PARSE_IMG_MAX_EDGE", "1280"))
    PARSE_FALLBACK_EDGE: int = int(os.getenv("PARSE_FALLBACK_EDGE", "1568"))

    # --- Facebook Messenger webhook ---
    FB_VERIFY_TOKEN: str = os.getenv("FB_VERIFY_TOKEN", "set-a-random-verify-token")
    FB_APP_SECRET: str = os.getenv("FB_APP_SECRET", "")   # used to verify payloads
    FB_PAGE_TOKEN: str = os.getenv("FB_PAGE_TOKEN", "")   # used later to send replies
    FB_GRAPH_VERSION: str = os.getenv("FB_GRAPH_VERSION", "v21.0")  # Send API version
    # Tag manual agent replies with HUMAN_AGENT to reach the customer up to 7
    # days after their last message (instead of 24h). Turn ON only AFTER the
    # "Human Agent" permission is approved on the Meta app, else sends error out.
    HUMAN_AGENT_ENABLED: bool = os.getenv("HUMAN_AGENT_ENABLED", "") not in ("", "0", "false", "False")
    # Wait this many seconds before replying, so a burst of messages (e.g. text
    # then screenshot as separate events) yields ONE reply on the merged state.
    ACK_DEBOUNCE_SECONDS: float = float(os.getenv("ACK_DEBOUNCE_SECONDS", "5"))
    # In the human-support lane, acknowledge receipt at most once per this many
    # seconds per customer (default 1 hour) so a chatty client isn't spammed.
    HUMAN_ACK_INTERVAL: float = float(os.getenv("HUMAN_ACK_INTERVAL", "3600"))
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

    # --- Client portal (espace client, passwordless magic links) ---
    # Absolute base URL used to build magic links sent over Messenger/email.
    PUBLIC_BASE_URL: str = os.getenv(
        "PUBLIC_BASE_URL", "https://duvoyageurbackend-production.up.railway.app").rstrip("/")
    # How long a magic login link stays valid (default 30 minutes).
    PORTAL_LINK_MAX_AGE: int = int(os.getenv("PORTAL_LINK_MAX_AGE", str(60 * 30)))
    # How long a portal session lasts once logged in (default 30 days).
    PORTAL_SESSION_MAX_AGE: int = int(os.getenv("PORTAL_SESSION_MAX_AGE", str(60 * 60 * 24 * 30)))

    # --- Social login for the client portal (OAuth). Each button only shows
    # once its keys are set. Redirect URIs to register with each provider:
    #   {PUBLIC_BASE_URL}/portail/auth/google/callback
    #   {PUBLIC_BASE_URL}/portail/auth/facebook/callback
    GOOGLE_CLIENT_ID: str = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
    # Facebook Login can live on the same Meta app as Messenger: the secret
    # defaults to FB_APP_SECRET when FB_LOGIN_APP_SECRET isn't set.
    FB_LOGIN_APP_ID: str = os.getenv("FB_LOGIN_APP_ID", "")
    FB_LOGIN_APP_SECRET: str = os.getenv("FB_LOGIN_APP_SECRET", "") or os.getenv("FB_APP_SECRET", "")

    # --- Portal AI travel assistant (Claude API) ---
    # AI travel agent in the portal: OFF (cancelled 2026-10 — too expensive).
    # Off = portal home is the DMO engine and every assistant endpoint is closed
    # (no Claude / Google / Pexels call). Code kept; set to 1 to revive.
    PORTAL_ASSISTANT_ENABLED: bool = os.getenv("PORTAL_ASSISTANT_ENABLED", "0") not in ("0", "false", "False", "")
    PORTAL_ASSISTANT_MODEL: str = os.getenv("PORTAL_ASSISTANT_MODEL", "claude-sonnet-5-5")
    # Which model answers members: "anthropic" (Claude) or "openai" (ChatGPT).
    PORTAL_ASSISTANT_PROVIDER: str = os.getenv("PORTAL_ASSISTANT_PROVIDER", "anthropic")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_ASSISTANT_MODEL: str = os.getenv("OPENAI_ASSISTANT_MODEL", "gpt-5.5")
    OPENAI_REASONING_EFFORT: str = os.getenv("OPENAI_REASONING_EFFORT", "low")
    # Emails allowed on the blind Claude vs ChatGPT test page (comma-separated).
    ASSISTANT_TESTERS: str = os.getenv("ASSISTANT_TESTERS", "")
    # Card photos for hotels NOT in our catalogue: the source page's share
    # image (1) or none (0). Catalogue hotels always use our own photos.
    PORTAL_ASSISTANT_WEB_IMAGES: bool = os.getenv("PORTAL_ASSISTANT_WEB_IMAGES", "1") not in ("0", "false", "False", "")
    # Answer length: "complet" (detailed agent-style advice) or "concis".
    PORTAL_ASSISTANT_STYLE: str = os.getenv("PORTAL_ASSISTANT_STYLE", "complet")
    PORTAL_ASSISTANT_DAILY_MAX: int = int(os.getenv("PORTAL_ASSISTANT_DAILY_MAX", "40"))
    # Web search (Claude server tool, ~$10 / 1000 searches + result tokens).
    PORTAL_ASSISTANT_SEARCHES_PER_ANSWER: int = int(os.getenv("PORTAL_ASSISTANT_SEARCHES_PER_ANSWER", "8"))
    PORTAL_ASSISTANT_DAILY_SEARCHES: int = int(os.getenv("PORTAL_ASSISTANT_DAILY_SEARCHES", "60"))
    # Web search scope. Empty = the whole web for INFORMATION (reviews, guides,
    # hotel sites), minus competing agencies (blocked list). Set an allow-list
    # (comma-separated bare domains) to restrict search to those sites only.
    PORTAL_ASSISTANT_SEARCH_DOMAINS: str = os.getenv("PORTAL_ASSISTANT_SEARCH_DOMAINS", "")
    PORTAL_ASSISTANT_BLOCKED_DOMAINS: str = os.getenv(
        "PORTAL_ASSISTANT_BLOCKED_DOMAINS",
        "expedia.ca,expedia.com,redtag.ca,itravel2000.com,flightcentre.ca,selloffvacations.com,"
        "travelzoo.com,kayak.com,kayak.ca,hotels.com,booking.com,priceline.com,trivago.ca,"
        "trivago.com,agoda.com,cheapflights.ca,voyagesarabais.com,clubvoyages.com,"
        "vacancesinterac.com,tripcentral.ca,travelocity.ca,travelocity.com,orbitz.com")
    # Progressive profiling: a cheap model checks the trip request first; if
    # essentials are missing, a short form is shown (no web search, no Google).
    PORTAL_ASSISTANT_TRIAGE: bool = os.getenv("PORTAL_ASSISTANT_TRIAGE", "1") not in ("0", "false", "False", "")
    PORTAL_ASSISTANT_TRIAGE_MODEL: str = os.getenv("PORTAL_ASSISTANT_TRIAGE_MODEL", "claude-haiku-5-5")
    # Claude extended thinking budget (tokens) before answering; 0 = off.
    PORTAL_ASSISTANT_THINKING: int = int(os.getenv("PORTAL_ASSISTANT_THINKING", "0"))
    # Google Places API (New): licensed hotel photos, rating, address, phone,
    # website, map link — displayed live with Google attribution. Off if empty.
    GOOGLE_PLACES_API_KEY: str = os.getenv("GOOGLE_PLACES_API_KEY", "")
    # Pexels API: generic « ambiance » photos for the assistant's DESTINATION
    # cards only (never hotels). Free: 200 req/h, 20 000/month. Off if empty.
    PEXELS_API_KEY: str = os.getenv("PEXELS_API_KEY", "")
    # Browser key (HTTP-referrer restricted; Maps Embed + Maps Static APIs) for maps.
    GOOGLE_MAPS_EMBED_KEY: str = os.getenv("GOOGLE_MAPS_EMBED_KEY", "")
    # The « Où sont les hôtels ? » static map is billed per view past the free
    # tier, so it is off unless set to 1 (the panel's embedded map is always free).
    GOOGLE_MAPS_STATIC: bool = os.getenv("GOOGLE_MAPS_STATIC", "0") in ("1", "true", "True")
    # "free" (photos, address, map: largest free quota) or "full" (+ rating, phone, website).
    GOOGLE_PLACES_FIELDS: str = os.getenv("GOOGLE_PLACES_FIELDS", "free")
    # Daily safety caps (0 = none); beyond them, wholesaler photos are used.
    # Free tier ≈ 1 000 photos and 5 000 basic searches per month.
    GOOGLE_PLACES_DAILY_PHOTOS: int = int(os.getenv("GOOGLE_PLACES_DAILY_PHOTOS", "30"))
    GOOGLE_PLACES_DAILY_SEARCHES: int = int(os.getenv("GOOGLE_PLACES_DAILY_SEARCHES", "150"))
    # Monthly hard caps (0 = automatic: 4 500 searches in "free" mode, 900 in
    # "full" mode; 900 photos) — kept under Google's monthly free usage.
    GOOGLE_PLACES_MONTHLY_SEARCHES: int = int(os.getenv("GOOGLE_PLACES_MONTHLY_SEARCHES", "0"))
    GOOGLE_PLACES_MONTHLY_PHOTOS: int = int(os.getenv("GOOGLE_PLACES_MONTHLY_PHOTOS", "0"))

    # --- Support: the portal's "Aide" tab sends clients to the Facebook page's
    # Messenger (the admin ticket queue is no longer used).
    MESSENGER_URL: str = os.getenv("MESSENGER_URL", "https://m.me/duvoyageur.ca")

    # Public site base URL, used for « Nos guides » links in the assistant
    # (switch to https://duvoyageur.ca once the domain is connected).
    SITE_BASE_URL: str = os.getenv("SITE_BASE_URL", "https://duvoyageur.netlify.app")

    # --- Softvoyage DMO package search engine (iframe in the portal) ---
    # Shown at /portail/recherche once the agency alias is set; until then the
    # page says "bientôt disponible".
    SIRRVA_ALIAS: str = os.getenv("SIRRVA_ALIAS", "big")
    SIRRVA_HOST: str = os.getenv("SIRRVA_HOST", "https://trs.sax.softvoyage.com")
    SIRRVA_CODE_AG: str = os.getenv("SIRRVA_CODE_AG", "trs")
    # Our search criteria -> engine URL parameter names ("ours=theirs", comma-separated).
    # Empty on purpose: tested 2026-10-22, querypackage.cgi ignores URL parameters
    # (real field names: gateway_dep, dest_dep, date_dep, duration, nb_adult, nb_child).
    SIRRVA_PARAM_MAP: str = os.getenv("SIRRVA_PARAM_MAP", "")

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
