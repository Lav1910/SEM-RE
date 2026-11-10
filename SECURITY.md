# Security

## Access
- **HBM only.** Every sign-in method accepts only addresses at `ALLOWED_EMAIL_DOMAIN` (homebuildermarketers.com).
  Existing sessions are dropped if an account's address falls outside the domain.
- **Email + password and one-time email codes are always available.**
  - A code request emails a 6-digit code and a single-use link.
  - Both are stored only as HMAC-SHA256 hashes, expire after 15 minutes, and stop working after 5 wrong tries or once a
    newer code is sent.
  - Codes are sent to any HBM address that isn't deactivated. A new address gets a hidden, unverified account that
    only becomes a real (Sales) account once the code or link is used, so only someone who can open that HBM inbox gets in.
  - Requests are limited to 3 per address and 10 per IP every 15 minutes.
  - After signing in with a code, a person can set or reset their password without the old one (for 15 minutes).
- **Google sign-in (optional)** (OpenID Connect, authorization code flow) runs as follows:
  - The app asks Google to show only HBM Workspace accounts (`hd`).
  - It uses a random `state` (expires after 10 minutes) and a `nonce`.
  - It checks the returned ID token's audience, issuer, expiry, nonce, `email_verified` and Workspace domain.
  - The token is taken directly from Google's token endpoint over TLS (OIDC Core 3.1.3.7).
  - A changed Google account ID for a known email is refused.
  - Set the consent screen to **Internal** so Google enforces the Workspace as well.
- **Who has an account:** anyone with an HBM address can sign in without being added first. A first Google sign-in, or a
  first correct emailed code, creates a **Sales** account (logged as "self sign-up" in the security log). Admins can
  still add people ahead of time, change roles, and **deactivate** anyone; deactivated people can't sign in by any
  method. `ADMIN_EMAILS` creates the first admin at startup.
- **Roles:** Admin and Sales, defined in `webapp/auth.py` (`PERMISSIONS`) and enforced on the server:
  - Admin-only routes return 403 to Sales.
  - Cost figures and raw data are hidden from Sales.
  - Admins can't change their own access.
- **Passwords:**
  - Hashed with PBKDF2-SHA256 at 600,000 rounds, with a 12-character minimum.
  - Failed sign-ins take the same time whether or not the email exists.
  - Rate limited to 8 failures per IP and 5 per email in 15 minutes.

## Sessions and requests
- Signed session cookie (`SECRET_KEY`): HttpOnly and SameSite=Lax. With HTTPS (`PUBLIC_URL`) it's also Secure and
  `__Host-` prefixed, with HSTS on. Sessions last 7 days, and the account is re-checked on every request.
- A CSRF token is required on every POST and compared in constant time.
- Headers:
  - Content-Security-Policy (`default-src 'self'`, `frame-ancestors 'none'`, `object-src 'none'`, `form-action 'self'`).
  - X-Frame-Options DENY, nosniff, Referrer-Policy same-origin, Permissions-Policy, COOP.
  - `Cache-Control: no-store` on signed-in pages; `noindex` everywhere.
- Every audit route checks the signed-in user's access. Shared report links use 144-bit random tokens and can be
  turned off per audit.
- Request bodies are capped at 2 MB. Redirect targets are limited to this site.

## Outbound requests (SSRF)
- Audits fetch user-supplied sites and every link in their sitemaps.
- `aeo/netsafe.py` allows only http/https on normal web ports, to hosts whose DNS answers are all public addresses,
  and checks every redirect hop.
- Cloud metadata (169.254.169.254), localhost and private networks are refused.

## Content
- AI answers and generated content are rendered with a Markdown renderer that escapes everything first and allows
  only http/https links. Templates auto-escape.
- Generated content uses only facts read from the site. Unknowns are marked `[confirm: …]`.

## Secrets
- API keys come from environment variables (or `~/.config/hbm-aeo/keys.env`, mode 600, for local use). They're
  never written to audits, logs or the repo. `.gitignore` excludes `.env`, keys, the database and client data.
- Rotate any key that was ever pasted into a chat, email or ticket.

## Logging
- Admin → **Security log** records sign-ins, failed and blocked attempts, account creation, approvals, role changes,
  deactivations and password changes, with the IP address.

## AI agents (MCP)
- `/mcp` lets AI agents (Claude, ChatGPT, Grok, Cursor) use the app as the person who connected them, with that person's
  role and access. Code: `webapp/mcp.py`.
- Sign-in is OAuth 2.1: authorization code with PKCE (S256 only), dynamic client registration and refresh tokens that
  rotate on every use. Redirect addresses must be https, loopback (any port) or an app scheme, and must match the
  registered ones exactly. The Allow page needs a normal signed-in session and a CSRF token.
- Personal access tokens (Settings › AI connections) are shown once. Every token is stored only as a SHA-256 hash and
  can be revoked at once. Access tokens expire after 24 hours; connections unused for 90 days must sign in again.
- Agent-started audits count toward the spending cap and a per-person daily limit (Admin › AI connections, default 20).
  Each person is limited to 90 agent actions a minute, every action is logged, and admins can turn the feature off.
- File links given to agents are signed, tied to the person and expire after an hour.

## Reporting a problem
Tell the HBM admin (lav@homebuildermarketers.com). Don't post details in public channels.
