"""Change 86: credit left on the paid services, on the Admin page."""
import os as _os, tempfile as _tf
_os.environ.setdefault("HBM_KV_DIR", _tf.mkdtemp(prefix="hbm-kv-test-"))
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("HBM_DATA_DIR", tempfile.mkdtemp(prefix="hbm-test-"))

from aeo import balances, config, kv  # noqa: E402

KEYS = {"DATAFORSEO_LOGIN": "me@x.com", "DATAFORSEO_PASSWORD": "pw-secret", "SEMRUSH_API_KEY": "a" * 32,
        "OPENAI_API_KEY": "sk-secret", "GEMINI_API_KEY": None, "PERPLEXITY_API_KEY": "pplx", "DATAWISE_TOKEN": None}


def dfs_body(balance):
    return json.dumps({"tasks": [{"result": [{"money": {"balance": balance, "total": 51}}]}]})


class Balances(unittest.TestCase):
    def setUp(self):
        kv.update("balances", lambda _o: {}, {})
        self.keys = mock.patch.object(config, "key", side_effect=lambda n: KEYS.get(n))
        self.keys.start()

    def tearDown(self):
        self.keys.stop()

    def test_dataforseo_states(self):
        for bal, state in ((29.38, "good"), (12.0, "warn"), (3.5, "bad")):
            with mock.patch.object(balances, "_get", return_value=dfs_body(bal)):
                r = balances.dataforseo()
            self.assertEqual((r["value"], r["state"]), (round(bal, 2), state))

    def test_errors_never_show_keys(self):
        err = urllib.error.HTTPError("https://api.x/?key=" + "a" * 32, 401, "Unauthorized", {}, None)
        with mock.patch.object(balances, "_get", side_effect=err):
            for r in (balances.dataforseo(), balances.semrush()):
                self.assertEqual(r["note"], "The provider rejected the key")
                self.assertNotIn("a" * 32, json.dumps(r)); self.assertNotIn("pw-secret", json.dumps(r))

    def test_semrush_units(self):
        with mock.patch.object(balances, "_get", return_value="8500\n"):
            r = balances.semrush()
        self.assertEqual((r["value"], r["state"]), (8500, "warn"))

    def test_cached_for_ten_minutes_unless_forced(self):
        with mock.patch.object(balances, "_get", side_effect=[dfs_body(20), "50000"]) as g:
            balances.readings(); balances.readings()
            self.assertEqual(g.call_count, 2)  # one reading of each provider, then the cache
        with mock.patch.object(balances, "_get", side_effect=[dfs_body(19), "49000"]):
            self.assertEqual(balances.readings(force=True)["dataforseo"]["value"], 19.0)

    def test_rows_never_turn_missing_into_zero(self):
        with mock.patch.object(balances, "_get", side_effect=[dfs_body(20), "50000"]):
            rows, t = balances.rows(force=True)
        by = {r["name"]: r for r in rows}
        self.assertIsNone(by["OpenAI"]["value"]); self.assertIn("doesn't share a balance", by["OpenAI"]["note"])
        self.assertEqual(by["Google Gemini"]["state"], "off")  # no key: not connected, not zero
        self.assertEqual(by["DataForSEO"]["value"], 20.0)


class AdminCard(unittest.TestCase):
    def test_admin_shows_the_card(self):
        from webapp import app as appmod, db
        app = appmod.app
        app.config["TESTING"] = True
        db.init()
        email = "balboss@homebuildermarketers.com"
        if not db.q("SELECT 1 FROM users WHERE email=?", (email,), one=True):
            db.x("INSERT INTO users (email, name, password_hash, is_admin, role, created_at) VALUES (?,?,?,?,?,?)",
                 (email, "B", "!", 1, "admin", time.time()))
        c = app.test_client()
        with c.session_transaction() as s:
            s["uid"], s["csrf"] = db.q("SELECT id FROM users WHERE email=?", (email,), one=True)["id"], "t"
        fake = ([{"name": "DataForSEO", "used_for": "Google", "state": "warn", "value": 12.5, "unit": "usd", "note": "n", "link": "https://app.dataforseo.com/"}], time.time())
        with mock.patch.object(balances, "rows", return_value=fake):
            html = c.get("/admin").get_data(as_text=True)
        self.assertIn("Credits and balances", html); self.assertIn("$12.50", html); self.assertIn("Running low", html)
        with mock.patch.object(balances, "rows", return_value=fake) as r:
            resp = c.post("/admin", data={"csrf": "t", "action": "balances"})
        self.assertTrue(resp.headers["Location"].endswith("#balances")); r.assert_called_with(force=True)


if __name__ == "__main__":
    unittest.main()
