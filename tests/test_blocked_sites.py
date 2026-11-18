"""Change 26 (sites that turn away automated visitors still get audited) and change 27 (a platform's status resets
when its key changes)."""
import os as _os, tempfile as _tf
_os.environ.setdefault("HBM_KV_DIR", _tf.mkdtemp(prefix="hbm-kv-test-"))  # keep tests away from the real spend/health files
import copy
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("HBM_DATA_DIR", tempfile.mkdtemp(prefix="hbm-test-"))
SAMPLE = os.environ.get("HBM_SAMPLE_AUDIT") or os.path.join(ROOT, "data", "sample_audit.json")  # not in the repo (client data)


def _fake_fetch(code):
    return lambda url, timeout=25: (code, "text/html", f"<html><title>{code} Forbidden</title><body>{code}</body></html>", url)


class BlockedSite(unittest.TestCase):
    def setUp(self):
        from aeo import datawise, engines, kv, locations, site
        self.patches = [
            mock.patch.object(engines, "ask", side_effect=lambda en, q, m: {"engine": en, "question_id": q["id"], "status": "ok",
                                                                            "text": "", "citations": []}),  # no real AI calls
            mock.patch.object(site, "rea_available", return_value=False),
            mock.patch.object(site, "infer_market_llm", return_value=None),  # no real AI calls in tests
            mock.patch.object(site, "lookup_market", return_value=(None, None, None)),  # change 30 is tested on its own
            mock.patch.object(site, "sitemap_urls", return_value=[]),
            mock.patch.object(datawise, "available", return_value=False),
                        mock.patch("aeo.dfs_volume.available", return_value=False),  # no real DataForSEO calls in tests
            mock.patch.object(locations, "find", side_effect=lambda p, m: [{"label": f"{m['city']}, {m['state_code']}", "main": True}]),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_name_from_domain(self):
        from aeo.site import name_from_domain
        self.assertEqual(name_from_domain("tourmalinebuilders.com"), "Tourmaline Builders")
        self.assertEqual(name_from_domain("www.fischerhomes.com"), "Fischer Homes")
        self.assertEqual(name_from_domain("chris-gorman-homes.com"), "Chris Gorman Homes")

    def test_blocked_site_keeps_going(self):
        import audit
        from aeo import site
        with mock.patch.object(site, "fetch", side_effect=_fake_fetch(403)):
            S = audit.prepare("https://tourmalinebuilders.com/", "Cincinnati", "OH", max_q=6, only=[], use_semrush=False)
        self.assertEqual(S["profile"]["site_blocked"], 403)
        self.assertEqual(S["profile"]["name"], "Tourmaline Builders")
        self.assertEqual(S["checks"], [])  # no false "fail" results from a page we couldn't read
        self.assertTrue(S["questions"])

    def test_blocked_site_asks_for_market(self):
        import audit
        from aeo import site
        with mock.patch.object(site, "fetch", side_effect=_fake_fetch(403)):
            with self.assertRaises(audit.AuditError) as cm:
                audit.prepare("https://tourmalinebuilders.com/", max_q=6, only=[], use_semrush=False)
        self.assertIn("city and state", str(cm.exception))  # the audit page shows the city/state form for this
        self.assertIn("turns away automated visitors", str(cm.exception))

    def test_missing_page_still_stops(self):
        import audit
        from aeo import site
        with mock.patch.object(site, "fetch", side_effect=_fake_fetch(404)):
            with self.assertRaises(audit.AuditError) as cm:
                audit.prepare("https://nosuchbuilder.com/", "Cincinnati", "OH", max_q=6, only=[], use_semrush=False)
        self.assertIn("Check the address", str(cm.exception))  # change 61 wording

    @unittest.skipUnless(os.path.exists(SAMPLE), "needs a sample audit (set HBM_SAMPLE_AUDIT)")
    def test_report_says_not_checked(self):
        from aeo import render
        from webapp import views
        A = json.load(open(SAMPLE))
        A["profile"]["site_blocked"], A["checks"] = 403, []
        A["analysis"]["kpis"].update(checks_passed=0, checks_total=0)
        html = render.render(A, {"V": views.build(A, "all", clean=True), "chart": views.chart(A, [])})
        self.assertIn("Website checks not run", html)
        self.assertNotIn("0 <small>of 0</small>", html)


class KeyChangeResetsStatus(unittest.TestCase):
    def test_label_resets_when_key_changes(self):
        from aeo import health
        from webapp import status
        with mock.patch.dict(os.environ, {"PERPLEXITY_API_KEY": "old-key-1"}):
            health.note("perplexity", False, "HTTP 401: invalid api key")
            self.assertEqual(status.current()["perplexity"]["label"], "Key rejected")
        with mock.patch.dict(os.environ, {"PERPLEXITY_API_KEY": "new-key-2"}):
            self.assertEqual(status.current()["perplexity"]["label"], "Not tested yet")
            health.note("perplexity", True)
            self.assertEqual(status.current()["perplexity"]["label"], "Connected")

    def test_fingerprint_is_not_the_key(self):
        from aeo import health
        with mock.patch.dict(os.environ, {"PERPLEXITY_API_KEY": "pplx-secret-value"}):
            kp = health.key_print("perplexity")
        self.assertEqual(len(kp), 12)
        self.assertNotIn("secret", kp)


if __name__ == "__main__":
    unittest.main()
