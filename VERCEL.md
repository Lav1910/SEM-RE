# Deploying on Vercel (branch `vercel-port`)

This branch runs the same app on Vercel. The `main` branch (tag `v1.0`) stays as it is for Render, Docker or a VPS.
This branch also still runs there: with no `DATABASE_URL` it falls back to SQLite and background threads.

## What changed for Vercel

| The app needs | On Render / a VPS | On Vercel (this branch) |
|---|---|---|
| A database | SQLite file on a disk | **Postgres** (Neon, from Vercel's Storage tab). The app switches automatically when `DATABASE_URL` is set. |
| Audit files (report, answers, PDFs) | Folder on the disk | Stored in the same Postgres database (`audit_files` table). |
| 3–5 minute audits in the background | Background threads | **Resumable steps.** `/api/jobs/tick` works for up to 4 minutes, saves its place and starts the next tick. The progress screen and a once-a-minute Vercel Cron also start ticks, so nothing gets stuck. |
| Spending cap, platform status, sign-in limits | Files and memory | Postgres, shared by every Vercel instance. |
| PDF reports | Chrome on the server | `api/pdf.js`, a Node function with serverless Chromium. If it ever fails, Export opens a print-ready page (Save as PDF). |

## Set it up (about 15 minutes)

1. **Import the repo:** vercel.com → **Add New → Project** → import **Lav1910/SEM-RE**.
   Framework preset: **Other**. Leave the build settings empty.
2. **Production branch:** Project → **Settings → Git → Production Branch** → `vercel-port`.
   Until it's merged into `main`, the `main` branch keeps serving Render.
3. **Database:** Project → **Storage → Create Database → Neon (Postgres)** → connect it to this project.
   Vercel adds `DATABASE_URL` / `POSTGRES_URL` for you.
4. **Environment variables:** Project → **Settings → Environment Variables**. Add these for Production:

   | Name | Value |
   |---|---|
   | `SECRET_KEY` | A long random string: `python3 -c "import secrets; print(secrets.token_hex(32))"` |
   | `CRON_SECRET` | Another long random string. It protects the job runner, and Vercel Cron sends it automatically. |
   | `PUBLIC_URL` | `https://<your-project>.vercel.app`, or your own domain once it's added |
   | `ADMIN_EMAILS` | `lav@homebuildermarketers.com` |
   | `ALLOWED_EMAIL_DOMAIN` | `homebuildermarketers.com` |
   | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `MAIL_FROM` | The mailbox that sends sign-in codes (see README) |
   | `OPENAI_API_KEY`, `GEMINI_API_KEY`, `PERPLEXITY_API_KEY`, `DATAFORSEO_LOGIN`, `DATAFORSEO_PASSWORD`, `SEMRUSH_API_KEY`, `DATAWISE_TOKEN` | Your API keys. Type them in yourself. |

5. **Deploy:** Deployments → **Redeploy**. Open the URL, type your email and click **Email me a sign-in code**.
6. **Bring over existing audits and users (optional):** on the computer that has the `appdata` folder, run:
   ```bash
   DATABASE_URL='<the non-pooled URL from Vercel → Storage → .env.local>' python tools/migrate_to_postgres.py --from appdata
   ```
   It's safe to run twice; rows that already exist are skipped.

## Plan

- **Works on Hobby (free) and Pro.** The safety-net cron in `vercel.json` runs once a day, which Hobby allows. Audits
  don't depend on it: the app starts ticks itself and the progress screen keeps them going.
- **On Pro (~$20/month, recommended for a team):** you can change the cron to every minute (`"* * * * *"`) for a
  faster safety net. Hobby is meant for personal, non-commercial use.
- **Neon Postgres:** the free tier is enough to start. Audits are about 0.3–1 MB each in the database.

## Checks done before handing over

- All 31 automated tests pass on both backends (SQLite, and Postgres in serverless mode). They cover sign-in by
  password and by emailed code, roles, CSRF, the job runner continuing an audit across ticks, leases, and the
  Content Generator.
- Real quick-test audits ran end to end in Vercel mode against Postgres, including one forced to continue across
  separate 45-second ticks with nobody watching the page. All pages and the PDF export were checked afterwards.
- The copy script moved the real local data (users, 14 audits, files, generated content) into Postgres.
- Not testable from a Mac without a Vercel account: `api/pdf.js` (it needs Vercel's Linux runtime). Check it after the
  first deploy by exporting a PDF. If it fails, the print-ready page appears instead, so nothing breaks.
