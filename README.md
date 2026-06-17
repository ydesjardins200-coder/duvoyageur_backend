# Du Voyageur — Backend & Admin

FastAPI service on Railway. Receives customer trips from the Netlify form
(`/intake`) and the Facebook Messenger webhook (`/webhook`), parses them into
clean cases with Claude, stores them, and serves an authed admin panel.

See `.env.example` for required variables. Start command is in `Procfile`.
Set `DATABASE_URL` to Supabase or Railway Postgres (SQLite locally by default).

## Endpoints
- `GET /webhook` / `POST /webhook` — Messenger verification + inbound messages
- `POST /intake` — the Netlify form posts a TripRequest here
- `GET /admin` — login form (session cookie)
- `GET /admin/cases` — case list (requires login)

## Run locally
    pip install -r requirements.txt
    cp .env.example .env   # set ADMIN_PASSWORD and SECRET_KEY
    uvicorn main:app --reload
