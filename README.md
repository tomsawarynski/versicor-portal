# Versicor Customer Forecast Portal (public)

Public-facing Flask app. Customers sign in by email magic-link and enter
weekly forecasts against their own parts only. Holds no sensitive shop data.

## Deploy on Render
1. Push this folder to a Git repo.
2. Render -> New -> Blueprint -> point at the repo. `render.yaml` provisions
   the web service + free Postgres and auto-generates SECRET_KEY and SYNC_API_KEY.
3. After first deploy, set in the service's Environment:
   - PORTAL_BASE_URL = your https URL (e.g. https://versicor-portal.onrender.com)
   - RESEND_API_KEY  = from resend.com (verify a sending domain first)
   - MAIL_FROM       = "Versicor Portal <portal@goversicor.com>"
4. Copy the generated SYNC_API_KEY into the shop sync `.env`.

Without RESEND_API_KEY the magic link is logged to the server console
(handy for a first smoke test before email is wired up).

## Local dev
    pip install -r requirements.txt
    python app.py            # http://localhost:5000
    python seed_dev.py       # one customer + buyer@gkn.example + 3 parts
    # POST email on /login, copy the magic link from the console.

## Security model
- Public surface = this app only. It stores customer emails, a parts mirror,
  and forecasts. No costs, margins, or inventory.
- Sync endpoints (/api/sync/*) require X-API-Key and are called OUTBOUND by
  the shop. The portal never connects in to the shop.
- Magic-link tokens are signed + 15-min TTL. Login never reveals whether an
  email exists. Customers only ever see their own customer_id's parts.
