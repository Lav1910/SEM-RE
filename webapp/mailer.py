"""Sends sign-in emails over SMTP (Google Workspace, Microsoft 365, SendGrid, Postmark, Resend, Mailgun... all speak SMTP).

Settings: SMTP_HOST, SMTP_PORT (587 STARTTLS or 465 SSL), SMTP_USER, SMTP_PASSWORD, MAIL_FROM.
Without SMTP settings and outside production, the message is written to the server log instead (local testing only).
"""
import html
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

log = logging.getLogger("hbm.mail")


def configured():
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("MAIL_FROM"))


def send(to, subject, text, html_body):
    if not configured():
        if os.environ.get("APP_ENV") == "production":
            raise RuntimeError("Email isn't set up on the server (SMTP settings missing).")
        log.warning("EMAIL NOT SENT (no SMTP settings; local testing). To: %s | %s\n%s", to, subject, text)
        return False
    msg = EmailMessage()
    msg["From"] = formataddr(("AEO Snapshot", os.environ["MAIL_FROM"]))
    msg["To"], msg["Subject"], msg["Message-ID"] = to, subject, make_msgid(domain=os.environ["MAIL_FROM"].split("@")[-1])
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587"))
    ctx = ssl.create_default_context()
    if port == 465:
        s = smtplib.SMTP_SSL(host, port, context=ctx, timeout=20)
    else:
        s = smtplib.SMTP(host, port, timeout=20); s.starttls(context=ctx)
    with s:
        if os.environ.get("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        s.send_message(msg)
    return True


def sign_in_email(to, name, code, link, minutes):
    first = html.escape((name or "").split(" ")[0] or "there")
    text = (f"Hi {first},\n\nYour sign-in code for AEO Snapshot is: {code}\n\nOr open this link to sign in:\n{link}\n\n"
            f"The code and link work once and expire in {minutes} minutes. If you didn't ask for this, ignore this email.\n\n"
            "Home Builder Marketers")
    body = f"""<div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;color:#16302F">
  <div style="background:#0C3335;padding:18px 22px;border-radius:12px 12px 0 0;color:#fff;font-weight:bold;letter-spacing:.04em">HOME BUILDER MARKETERS</div>
  <div style="border:1px solid #E3EBEC;border-top:0;border-radius:0 0 12px 12px;padding:22px">
    <p>Hi {first},</p>
    <p>Your sign-in code for <b>AEO Snapshot</b>:</p>
    <p style="font-size:30px;font-weight:bold;letter-spacing:.3em;color:#0A686A;margin:10px 0 18px">{html.escape(code)}</p>
    <p><a href="{html.escape(link)}" style="display:inline-block;background:#0A686A;color:#fff;text-decoration:none;padding:11px 18px;border-radius:8px;font-weight:bold">Sign in</a></p>
    <p style="color:#4F5F61;font-size:13px">The code and link work once and expire in {minutes} minutes. If you didn't ask for this, ignore this email.</p>
  </div>
  <p style="color:#7A898B;font-size:11px;text-align:center;letter-spacing:.12em">HOME BUILDER MARKETERS · HomeBuilderMarketers.com</p>
</div>"""
    return send(to, f"Your AEO Snapshot sign-in code: {code}", text, body)
