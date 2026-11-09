#!/usr/bin/env python3
"""Admin commands for the web app.

  python3 manage.py add-user --email you@homebuildermarketers.com --name "Your Name" --admin   # prompts for a password
  python3 manage.py list-users
  python3 manage.py reset-password --email someone@homebuildermarketers.com
  python3 manage.py deactivate --email someone@homebuildermarketers.com
"""
import argparse
import getpass
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from werkzeug.security import generate_password_hash  # noqa: E402

from webapp import db  # noqa: E402


def ask_password():
    while True:
        p1 = getpass.getpass("Password (12+ characters, hidden): ")
        if len(p1) < 12:
            print("Too short."); continue
        if getpass.getpass("Repeat password: ") != p1:
            print("Didn't match."); continue
        return p1


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add-user"); a.add_argument("--email", required=True); a.add_argument("--name", required=True); a.add_argument("--admin", action="store_true")
    sub.add_parser("list-users")
    r = sub.add_parser("reset-password"); r.add_argument("--email", required=True)
    d = sub.add_parser("deactivate"); d.add_argument("--email", required=True)
    args = ap.parse_args()
    db.init()
    if args.cmd == "add-user":
        from webapp import auth
        email = args.email.strip().lower()
        if not auth.allowed_email(email):
            raise SystemExit(f"Only {auth.ALLOWED_DOMAIN} addresses can have an account (set ALLOWED_EMAIL_DOMAIN to change).")
        if db.q("SELECT 1 FROM users WHERE email=?", (email,), one=True):
            raise SystemExit("That email already has an account.")
        db.x("INSERT INTO users (email, name, password_hash, is_admin, role, created_at) VALUES (?,?,?,?,?,?)",
             (email, args.name, generate_password_hash(ask_password(), method="pbkdf2:sha256:600000"), 1 if args.admin else 0,
              "admin" if args.admin else "sales", time.time()))
        print(f"Added {email}{' (admin)' if args.admin else ''}.")
    elif args.cmd == "list-users":
        for u in db.q("SELECT * FROM users ORDER BY name"):
            print(f"{u['email']:40} {u['name']:25} {'admin' if u['is_admin'] else 'sales':6} {'active' if u['active'] else 'deactivated'}")
    elif args.cmd == "reset-password":
        if not db.q("SELECT 1 FROM users WHERE email=?", (args.email.lower(),), one=True):
            raise SystemExit("No such user.")
        db.x("UPDATE users SET password_hash=? WHERE email=?", (generate_password_hash(ask_password(), method="pbkdf2:sha256:600000"), args.email.lower()))
        print("Password updated.")
    elif args.cmd == "deactivate":
        db.x("UPDATE users SET active=0 WHERE email=?", (args.email.lower(),))
        print("Deactivated.")


if __name__ == "__main__":
    main()
