# HBM AI Visibility Audits

A web app for the Home Builder Marketers sales team. A salesperson enters a US home builder's website and gets an
AI visibility (AEO) audit:

- What ChatGPT, Gemini, Perplexity, Google AI Overviews and Google AI Mode say when local buyers ask for a builder.
- Which competitors and sources those answers name.
- City-by-city visibility for each place the builder serves.
- Website checks.
- A Strategy section with a ranked plan, a content generator and technical fixes.
- An HBM-branded PDF report and Loom talking points.

Only `@homebuildermarketers.com` accounts can sign in, with an email and password or a one-time code or link sent
by email. Admins add people and choose their role.

---

## 1. Rebuild it anywhere in 5 minutes

You need Docker, or Python 3.9+ and Chrome.

```bash
git clone https://github.com/Lav1910/SEM-RE.git && cd SEM-RE
cp .env.example .env          # fill in the values (see section 3)
docker build -t hbm-aeo .
docker run -p 8080:8080 -v hbm-data:/data --env-file .env hbm-aeo
```

Open http://localhost:8080. Everything the app stores (users, audits, PDFs) lives in the `/data` volume. Back that
up, and the code in this repo rebuilds the rest.

**Without Docker (a Mac or a laptop):**

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
python3 setup_keys.py                         # type each API key yourself (hidden input, saved with mode 600)
.venv/bin/python manage.py add-user --email you@homebuildermarketers.com --name "You" --admin
.venv/bin/python serve.py                     # http://127.0.0.1:8080
```

**On a Windows PC:**

1. Install **Python 3.12** from https://www.python.org/downloads/ and tick **"Add python.exe to PATH"** during setup.
   Install **Git** from https://git-scm.com/download/win, or download the repo as a ZIP from GitHub.
2. Get the code: `git clone https://github.com/Lav1910/SEM-RE.git`, or unzip the download.
3. Double-click **`run-windows.bat`** in the folder. The first run sets everything up and asks for the API keys
   (typed hidden). Then it opens http://127.0.0.1:8080.
4. Sign in: type your email and click **Email me a sign-in code**. Locally, the code appears in the black window
   (no email is sent), and you can set a password after. `ADMIN_EMAILS` (set in the .bat file) makes that address an admin.

PDFs use Chrome or Edge, whichever is installed. Data is saved in the `appdata` folder next to the code. To bring
over existing audits and users, copy `appdata` from the other computer while the app is stopped.

## 2. Put it online

| Host | How | Cost |
|---|---|---|
| **Render** (recommended) | Render → New → **Blueprint** → choose this repo. `render.yaml` creates the web service and a 1 GB disk. Fill in the secrets it asks for. Every push to `main` deploys. | ~$7.25/mo |
| **Railway** | New project → Deploy from GitHub → add a Volume mounted at `/data` → add the variables from `.env.example`. | ~$5–10/mo |
| **Any Linux VPS** (IONOS, Fasthosts, DigitalOcean) | Install Docker, point a DNS record at the server, fill in `.env` (plus `DOMAIN=`), run `docker compose up -d`. Caddy gets HTTPS automatically (`deploy/Caddyfile`). | ~$5–12/mo |

These hosts won't work as-is: Vercel, Cloudflare Workers/Pages and shared web hosting. They have no lasting disk and
no long-running worker, and they can't run Chrome for PDFs.

**A custom address:** point `audit.homebuildermarketers.com` (CNAME) at the host and set `PUBLIC_URL` to it. If the
domain is on Cloudflare, you can add Cloudflare Access (free for up to 50 users) for an extra lock that allows only
@homebuildermarketers.com Google accounts.

## 3. Settings (environment variables)

Everything is in `.env.example`. The important ones:

| Variable | What it does |
|---|---|
| `PUBLIC_URL` | The address people open, e.g. `https://audit.homebuildermarketers.com`. Turns on secure cookies and HSTS, and builds the Google sign-in return address. |
| `SECRET_KEY` | A long random string that signs sessions (`python3 -c "import secrets; print(secrets.token_hex(32))"`). Render generates one. |
| `HBM_DATA_DIR` | The persistent folder for the database, audits and PDFs (`/data` in Docker). |
| `TRUSTED_PROXY` | `*` when the host's HTTPS proxy is in front (Render, Railway, Caddy), so the real visitor IP is used for rate limits. |
| `ALLOWED_EMAIL_DOMAIN` | `homebuildermarketers.com`. Nobody else can sign in by any method. |
| `ADMIN_EMAILS` | Comma-separated addresses created as Admin when the app starts. On a fresh server, the first admin just clicks "Email me a sign-in code". |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `MAIL_FROM` | The mailbox that sends sign-in codes (see below). Without it, people can only sign in with a password. |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Optional extra "Sign in with Google" (see below). |
| `OPENAI_API_KEY`, `GEMINI_API_KEY`, `PERPLEXITY_API_KEY`, `DATAFORSEO_LOGIN`, `DATAFORSEO_PASSWORD`, `SEMRUSH_API_KEY`, `DATAWISE_TOKEN` | Data providers. Enter them in the host's secret settings, and never commit them. |

### Setting up sign-in emails (needed for one-time codes)

Use a mailbox such as `no-reply@homebuildermarketers.com`:
- **Google Workspace:** turn on 2-Step Verification for that mailbox, then create an **App password**
  (myaccount.google.com → Security → App passwords). Set `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`,
  `SMTP_USER` and `MAIL_FROM` to the mailbox, and `SMTP_PASSWORD` to the app password.
- **Any other provider** (Microsoft 365, SendGrid, Postmark, Resend, Mailgun) works the same way with its SMTP details.

Locally, without SMTP settings, the code is printed in the server log instead.

### Setting up Google sign-in (optional, about 5 minutes, done once by an HBM Google Workspace admin)

1. Go to https://console.cloud.google.com → create a project (e.g. "HBM AI Visibility").
2. **APIs & Services → OAuth consent screen** → User type **Internal**. This limits sign-in to the HBM Workspace on Google's side too. Enter the app name and a support email.
3. **APIs & Services → Credentials → Create credentials → OAuth client ID** → type **Web application**.
4. **Authorized redirect URI:** `https://<your PUBLIC_URL>/auth/google/callback`. Add `http://localhost:8080/auth/google/callback` for local testing.
5. Copy the client ID and secret into `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` on the host, then redeploy.

## 4. Who can do what

| | Admin | Sales |
|---|:-:|:-:|
| Run audits, watch them run, see every audit screen (overview, competitors, prompts, site audit) | ✅ | ✅ |
| Strategy plan, content generator, technical fixes, PDF exports, share links, Loom notes, feedback | ✅ | ✅ |
| See the whole team's audits | ✅ | ✅ |
| Admin page: approve or reject people, change roles, deactivate, spending cap, platform status, feedback, security log | ✅ | — |
| Cost figures and raw audit data (JSON) | ✅ | — |

The permissions are defined in one place, `webapp/auth.py` → `PERMISSIONS`, so they're easy to change.

**How people get in:**
1. An admin opens **Admin → Team → Add a team member**: work email, name and role. A password is optional.
2. The new person opens the app, types their email and clicks **Email me a sign-in code**.
3. They enter the 6-digit code, or click the link in the email. Then they can set a password for next time.
4. From then on they can always sign in with **email + password** or with an **emailed code**. A forgotten password
   is just "Email me a sign-in code", then a new password.

If Google sign-in is turned on, a new person can also click **Sign in with Google**. They wait for an admin to
approve them, and people listed in `ADMIN_EMAILS` skip that step.

## 5. Security

See [SECURITY.md](SECURITY.md) for the full list. In short:
- Only HBM addresses can sign in: email + password, a one-time emailed code or link (single use, 15 minutes, 5 tries), or optionally Google.
- Codes go only to accounts an admin has added. New Google sign-ins need approval.
- CSRF protection on every form.
- Secure, HttpOnly session cookies; a strict Content-Security-Policy, HSTS and anti-framing headers.
- Login rate limits per IP and per email.
- Passwords hashed with PBKDF2-SHA256 (600k rounds).
- A guard stops audits from reaching private or internal network addresses.
- A security log of sign-ins and admin actions.
- No secrets in the repo.

## 6. How it works (map of the code)

```
audit.py            one audit end to end: read site → find service cities → questions → ask 5 AI platforms → analyse → report
aeo/
  site.py           read the builder's website (markup, phones, sitemap, robots, llms.txt, service-area links)
  locations.py      find and validate service cities (markup, community pages, "serving…" text, Gemini check)
  questions.py      local buyer questions (+3 per extra city, searched from that city)
  engines.py        ChatGPT, Gemini, Perplexity, Google AI Overviews/AI Mode (DataForSEO; Semrush fallback)
  analyze.py        who is named, citations, Maps 3-pack, scores, recommendations
  semrush.py        Semrush API (search volume, AI Overview history); datawise.py: keyword difficulty, Google profile
  strategy.py       Strategy plan, technical fixes, content generators (FAQ, blogs, city pages, llms.txt…)
  scope.py          Root domain / subdomain / subfolder / exact URL
  netsafe.py        blocks fetching private/internal addresses
  budget.py, health.py   spending cap; live platform status
  render.py         HBM-branded PDF report (tools/report.css)
webapp/
  app.py            Flask routes; auth.py sign-in and roles; db.py SQLite; jobs.py audit workers; strategy_jobs.py writers
  views.py          Semrush-style screens; templates/ and static/ for the UI
tests/              python -m unittest discover tests
```

## 7. Maintenance

- **Back up** `/data` (the `hbm-aeo.sqlite3` database and the `audits/` folder). On Render, use disk snapshots.
- **Spending cap:** Admin page, or `python spend.py`.
- **Users from the command line:** `python manage.py list-users | add-user | reset-password | deactivate`.
- **Run the tests:** `python -m unittest discover tests`.
