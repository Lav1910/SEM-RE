"""Several Google accounts for one Google source (change 87).

Business Profile and Analytics each keep a MAIN sign-in (settings key KEY, as before) and any number of EXTRA sign-ins
(KEY + "_extra", {email: record}). Every record keeps its refresh token encrypted (secretbox), never in plain text.

Why: client profiles and properties are shared with different HBM Google accounts (info@ sees most, seo@ sees a few
others). The directory of profiles/properties is built from every connected account, each row remembers the account that
sees it ("via"), and a client's data is read through that account:

    with ACC.use("seo@homebuildermarketers.com"):
        ...calls here use seo@'s sign-in...

Outside a use() block the main account is used, so everything written before this change behaves exactly as it did.
"""
import contextlib
import threading
import time

from aeo import kv


class Accounts:
    def __init__(self, key, label, not_connected, reauth):
        self.key, self.extra_key, self.label = key, key + "_extra", label
        self._NotConnected, self._Reauth = not_connected, reauth
        self._local = threading.local()
        self._access = {}  # email or "" -> {"token", "exp"}

    # ---------------------------------------------------------------- which account the current calls use
    def current(self):
        return getattr(self._local, "email", None)

    @contextlib.contextmanager
    def use(self, email):
        prev = self.current()
        self._local.email = (email or None)
        try:
            yield
        finally:
            self._local.email = prev

    def _primary(self):
        return kv.get(self.key) or {}

    def _extras(self):
        return kv.get(self.extra_key) or {}

    def _is_primary(self, email):
        p = self._primary()
        return not email or (p.get("enc") and (p.get("email") or "").lower() == email.lower())

    def _rec(self):
        cur = self.current()
        if self._is_primary(cur):
            return self._primary(), None
        return self._extras().get(cur.lower()) or {}, cur.lower()

    # ---------------------------------------------------------------- what's connected (never the token)
    @staticmethod
    def _summary(rec, main):
        return {**{k: rec.get(k) for k in ("email", "by", "connected_at", "state", "detail")}, "main": main}

    def stored(self):
        p = self._primary()
        return self._summary(p, True) if p.get("enc") else None

    def extras(self):
        return [self._summary(r, False) for _, r in sorted(self._extras().items()) if r.get("enc")]

    def all(self):
        """Every connected account, main first."""
        return ([self.stored()] if self.stored() else []) + self.extras()

    def emails(self):
        """Accounts that can be used now (not waiting for a new sign-in), main first. None stands for the main account
        when it has no email label."""
        return [a.get("email") for a in self.all() if a.get("state") != "reauth"]

    def connected(self):
        return bool(self.emails())

    # ---------------------------------------------------------------- saving and removing sign-ins
    def save(self, refresh_token, email, by, extra=False):
        """Store a sign-in. extra=True keeps the main account and adds this one beside it (same email: replaces it)."""
        from webapp.cc import secretbox
        rec = {"enc": secretbox.seal(refresh_token), "email": email, "by": by, "connected_at": time.time(),
               "state": "ok", "detail": None}
        p = self._primary()
        if extra and p.get("enc") and email and not self._is_primary(email):
            kv.update(self.extra_key, lambda old: {**(old or {}), email.lower(): rec}, {})
        else:
            kv.update(self.key, lambda _o: rec, {})
            if email:
                kv.update(self.extra_key, lambda old: {k: v for k, v in (old or {}).items() if k != email.lower()} or {}, {})
        self.cache_clear(email)

    def mark(self, state, detail=None):
        """Flag the account the current calls use (main unless inside use())."""
        _, cur = self._rec()
        if cur is None:
            kv.update(self.key, lambda old: None if not old or not old.get("enc") else {**old, "state": state, "detail": detail}, {})
        else:
            kv.update(self.extra_key, lambda old: None if cur not in (old or {}) else
                      {**old, cur: {**old[cur], "state": state, "detail": detail}}, {})

    def forget(self, email=None):
        """Remove one sign-in (the main one when email is None). When the main one goes, the first extra takes its place."""
        if self._is_primary(email):
            extras = self._extras()
            if extras:
                first = sorted(extras)[0]
                promoted = extras.pop(first)
                kv.update(self.key, lambda _o: promoted, {})
                kv.update(self.extra_key, lambda _o: extras, {})
            else:
                kv.update(self.key, lambda _o: {}, {})
        else:
            kv.update(self.extra_key, lambda old: {k: v for k, v in (old or {}).items() if k != email.lower()}, {})
        self.cache_clear(email)

    def refresh_token(self):
        from webapp.cc import secretbox
        rec, _ = self._rec()
        tok = secretbox.open_(rec["enc"]) if rec.get("enc") else None
        if not tok:
            if rec.get("enc"):
                self.mark("reauth", "The stored Google sign-in can't be read any more (the app's secret key changed)")
                raise self._Reauth("stored sign-in unreadable")
            raise self._NotConnected(f"{self.label} not connected" + (f" for {self.current()}" if self.current() else ""))
        return tok

    # ---------------------------------------------------------------- short-lived access tokens, one per account
    def _ckey(self, email=None):
        email = self.current() if email is None else email
        return "" if self._is_primary(email) else email.lower()

    def cache_get(self):
        a = self._access.get(self._ckey())
        return a["token"] if a and a["exp"] > time.time() else None

    def cache_set(self, token, expires_in):
        self._access[self._ckey()] = {"token": token, "exp": time.time() + int(expires_in or 3600) - 60}

    def cache_clear(self, email=None):
        if email is None and self.current() is None:
            self._access.clear()
        else:
            self._access.pop(self._ckey(email), None)
