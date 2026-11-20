"""Offline tests. Fixtures copy the response shapes in each provider's docs (Nov 2026); the answer
text is invented test content, not real AI output. Run: python3 -m unittest discover tests"""
import os as _os, tempfile as _tf
_os.environ.setdefault("HBM_KV_DIR", _tf.mkdtemp(prefix="hbm-kv-test-"))  # keep tests away from the real spend/health files
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aeo import analyze, engines, questions, site  # noqa: E402

OPENAI = {"output": [
    {"type": "web_search_call", "id": "ws_1", "status": "completed", "action": {"type": "search", "query": "custom home builders cincinnati"}},
    {"type": "message", "role": "assistant", "content": [{"type": "output_text",
        "text": "Top picks: 1. Wieland Builders, known for luxury homes. 2. Justin Doyle Homes builds ranch homes. 3. JK Custom Homes.",
        "annotations": [{"type": "url_citation", "start_index": 0, "end_index": 40, "url": "https://www.wielandbuilders.com/", "title": "Wieland"},
                        {"type": "url_citation", "start_index": 41, "end_index": 80, "url": "https://justindoylehomes.com/", "title": "JDH"}]}]}]}
GEMINI = {"steps": [
    {"type": "google_search_call", "arguments": {"queries": ["best builders cincinnati"]}},
    {"type": "model_output", "content": [{"type": "text", "text": "Chris Gorman Homes and Wieland Builders are popular choices.",
        "annotations": [{"type": "url_citation", "url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc", "title": "chrisgormanhomes.com",
                         "start_index": 0, "end_index": 30}]}]}]}
PPLX = {"output": [
    {"type": "search_results", "queries": ["x"], "results": [
        {"id": 1, "url": "https://www.houzz.com/professionals/home-builders/cincinnati", "title": "Houzz", "source": "web"},
        {"id": 2, "url": "https://www.yelp.com/search?find_desc=home+builders", "title": "Yelp", "source": "web"}]},
    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Consider The Bold Company or Potter House Group [1][2]."}]}],
    "usage": {"cost": {"total_cost": 0.0123}}}
DFS = {"cost": 0.0026, "tasks": [{"status_code": 20000, "result": [{"items": [
    {"type": "ai_overview", "markdown": "Popular builders include Justin Doyle Homes and Wieland Builders.",
     "references": [{"type": "ai_overview_reference", "source": "Yelp", "domain": "m.yelp.com", "url": "https://m.yelp.com/biz/x", "title": "Yelp"}],
     "items": [{"type": "ai_overview_element", "references": [{"url": "https://justindoylehomes.com/", "title": "JDH"}]}]},
    {"type": "local_pack", "title": "Wieland Builders", "url": "https://wielandbuilders.com/", "domain": "wielandbuilders.com"},
    {"type": "local_pack", "title": "JK Custom Homes", "url": "https://jkcustomhomes.net/"},
    {"type": "organic", "url": "https://justindoylehomes.com/", "rank_group": 1}]}]}]}


class ParserTests(unittest.TestCase):
    def test_openai(self):
        text, cites = engines.parse_openai(OPENAI)
        self.assertIn("Justin Doyle Homes", text)
        self.assertEqual([c["domain"] for c in cites], ["wielandbuilders.com", "justindoylehomes.com"])

    def test_gemini_redirect_uses_title_domain(self):
        text, cites = engines.parse_gemini(GEMINI)
        self.assertEqual(cites[0]["domain"], "chrisgormanhomes.com")

    def test_perplexity(self):
        text, cites, cost = engines.parse_perplexity(PPLX)
        self.assertEqual([c["domain"] for c in cites], ["houzz.com", "yelp.com"])
        self.assertAlmostEqual(cost, 0.0123)

    def test_dataforseo(self):
        text, cites, pack, organic, cost = engines.parse_dataforseo(DFS)
        self.assertIn("Wieland", text)
        self.assertEqual({c["domain"] for c in cites}, {"yelp.com", "justindoylehomes.com"})
        self.assertEqual([p["domain"] for p in pack], ["wielandbuilders.com", "jkcustomhomes.net"])
        self.assertEqual(organic[0]["rank"], 1)

    def test_dataforseo_error(self):
        with self.assertRaises(RuntimeError):
            engines.parse_dataforseo({"tasks": [{"status_code": 40501, "status_message": "Invalid Field: location_name"}]})


class SiteTests(unittest.TestCase):
    def test_robots(self):
        self.assertEqual(site.robots_blocks("User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nDisallow: /wp-admin/"), ["GPTBot"])
        self.assertEqual(site.robots_blocks("User-agent: *\nAllow: /"), [])

    def test_market_from_areas(self):
        p = {"areas_served": ["Greater Cincinnati", "Northern Kentucky", "Dayton"], "city": "Mason", "state": "Ohio"}
        self.assertEqual(site.market_from(p)["city"], "Cincinnati")
        self.assertEqual(site.market_from(p)["state_code"], "OH")


PROFILE = {"name": "Justin Doyle Homes", "domain": "justindoylehomes.com", "areas_served": ["Greater Cincinnati", "Northern Kentucky", "Dayton"],
           "services": ["custom homes", "luxury homes", "ranch homes", "build on your lot", "condos"], "city": "Mason", "state": "Ohio"}
MARKET = {"city": "Cincinnati", "state": "Ohio", "state_code": "OH"}


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.qs = questions.apply_volumes(questions.build(PROFILE, MARKET, 14), {"custom home builders cincinnati": 480})

    def test_questions_cover_areas_and_services(self):
        ids = [q["id"] for q in self.qs]
        self.assertIn("area_northern_kentucky", ids)
        self.assertIn("area_dayton", ids)
        self.assertIn("own_lot", ids)
        self.assertTrue(all(q["weight"] >= 10 for q in self.qs))

    def test_builder_extraction(self):
        gen = analyze.generic_rx(["Cincinnati", "Ohio", "OH"])
        names = analyze.find_builders("The best custom home builders in Cincinnati include Wieland Builders, "
                                      "Chris Gorman Homes and Robert Lucke Group. Many local builders offer ranch homes.", [], gen)
        self.assertEqual(names, ["Wieland Builders", "Chris Gorman Homes", "Robert Lucke Group"])

    def test_end_to_end_scoring(self):
        q = self.qs[1]  # "custom" question
        ans = []
        for en, resp in [("chatgpt", OPENAI), ("gemini", GEMINI), ("perplexity", PPLX)]:
            parse = {"chatgpt": engines.parse_openai, "gemini": engines.parse_gemini, "perplexity": engines.parse_perplexity}[en]
            out = parse(resp)
            ans.append(engines._record(en, q, text=out[0], citations=out[1]))
        t, c, pack, org, _ = engines.parse_dataforseo(DFS)
        ans.append(engines._record("google_aio", q, text=t, citations=c, local_pack=pack, organic=org))
        ans.append(engines._record("google_ai_mode", q, status="not_configured"))
        res = analyze.analyze(PROFILE, MARKET, [q], ans, [("good", "x", "y")], None)
        pe = res["per_engine"]
        self.assertEqual(pe["chatgpt"]["mentioned"], 1)
        self.assertEqual(pe["gemini"]["mentioned"], 0)
        self.assertEqual(pe["google_aio"]["cited"], 1)
        self.assertFalse(pe["google_ai_mode"]["measured"])
        self.assertEqual(res["kpis"]["mentioned"], 2)
        self.assertEqual(res["kpis"]["answers"], 4)
        chat = next(x for x in res["cells"] if x["engine"] == "chatgpt")
        self.assertEqual(chat["rank"], 2)  # named second, after Wieland Builders
        names = [b["name"] for b in res["leaderboard"]]
        self.assertIn("Wieland Builders", names)
        self.assertEqual(res["kpis"]["pack"], 0)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from aeo import budget, kv
        self.budget, self.kv = budget, kv
        self.orig = (budget.LEDGER, kv.DIR, kv.PG)
        budget.LEDGER = os.path.join(tempfile.mkdtemp(), "spend.json")
        kv.DIR, kv.PG = tempfile.mkdtemp(), False   # never touch the real ledger

    def tearDown(self):
        self.budget.LEDGER, self.kv.DIR, self.kv.PG = self.orig

    def test_cap_stops_calls_and_settles_real_cost(self):
        b = self.budget
        b.set_limit(0.20, reset=True)
        r1 = b.reserve("chatgpt")            # 0.15 reserved (change 59: real OpenAI web-search prices)
        self.assertIsNotNone(r1)
        self.assertIsNone(b.reserve("gemini"))  # 0.15 + 0.06 > 0.20 -> blocked
        b.settle(r1, 0.02)                   # real cost lower than the estimate
        self.assertAlmostEqual(b.status()["spent_usd"], 0.02)
        self.assertIsNotNone(b.reserve("gemini"))  # room again after settling

    def test_capped_call_returns_budget_status(self):
        b = self.budget
        b.set_limit(0.0, reset=True)
        os.environ["OPENAI_API_KEY"] = "test-not-a-real-key"
        try:
            rec = engines.ask("chatgpt", {"id": "t", "prompt": "x", "keyword": "x"}, {"city": "C", "state": "Ohio", "state_code": "OH"})
        finally:
            del os.environ["OPENAI_API_KEY"]
        self.assertEqual(rec["status"], "budget_reached")



class ScopeTests(unittest.TestCase):
    def test_scopes(self):
        from aeo import scope
        r = scope.make("https://www.jdh.com/communities/", "root")
        self.assertEqual(r["label"], "jdh.com")
        self.assertTrue(scope.matches("https://blog.jdh.com/x", r))
        self.assertFalse(scope.matches("https://notjdh.com/", r))
        sd = scope.make("blog.jdh.com", "subdomain")
        self.assertTrue(scope.matches("https://blog.jdh.com/a", sd)); self.assertFalse(scope.matches("https://jdh.com/a", sd))
        sf = scope.make("jdh.com/communities/", "subfolder")
        self.assertEqual(scope.semrush_target(sf), "jdh.com/communities/")
        self.assertTrue(scope.matches("https://www.jdh.com/communities/raleigh", sf))
        self.assertFalse(scope.matches("https://jdh.com/communities-old", sf))
        u = scope.make("jdh.com/about", "url")
        self.assertTrue(scope.matches("https://jdh.com/about/", u)); self.assertFalse(scope.matches("https://jdh.com/about/team", u))

if __name__ == "__main__":
    unittest.main()


class GoogleLocationFallback(unittest.TestCase):
    """Change 81: a town DataForSEO doesn't list is asked from its province, then the country."""

    def test_falls_back_to_province(self):
        from aeo import engines
        market = {"city": "Glenella", "state": "Manitoba", "state_code": "MB", "country": "CA"}
        self.assertEqual(engines._dfs_locations(market), ["Glenella,Manitoba,Canada", "Manitoba,Canada", "Canada"])
        sent = []

        def fake_post(url, payload, headers, retries=3):
            loc = payload[0]["location_name"]
            sent.append(loc)
            if loc.startswith("Glenella"):
                return {"tasks": [{"status_code": 40501, "status_message": "Invalid Field: 'location_name'."}]}
            return {"cost": 0.002, "tasks": [{"status_code": 20000, "result": [{"items": [{"type": "ai_overview", "markdown": "Answer", "references": []}]}]}]}
        with mock.patch.object(engines, "_post", fake_post), mock.patch.object(engines.config, "key", return_value="x"):
            rec = engines.ask_google_ai_mode({"id": "q1", "prompt": "best cabinet maker near me", "keyword": "cabinets"}, market)
        self.assertEqual((rec["status"], rec["location"]), ("ok", "Manitoba,Canada"))
        self.assertEqual(sent, ["Glenella,Manitoba,Canada", "Manitoba,Canada"])

    def test_other_errors_are_not_retried(self):
        from aeo import engines
        calls = []

        def fake_post(url, payload, headers, retries=3):
            calls.append(1)
            return {"tasks": [{"status_code": 40101, "status_message": "Internal SE Server Error."}]}
        with mock.patch.object(engines, "_post", fake_post), mock.patch.object(engines.config, "key", return_value="x"), \
                mock.patch("time.sleep"):
            with self.assertRaises(RuntimeError):
                engines.ask_google_aio({"id": "q1", "prompt": "p", "keyword": "k"}, {"city": "Austin", "state": "Texas", "state_code": "TX", "country": "US"})
        self.assertEqual(len(calls), 2)  # a temporary DataForSEO failure gets one more try, then the question is skipped

    def test_status_labels(self):
        from webapp import status
        self.assertEqual(status.friendly_error("DataForSEO 40101: Internal SE Server Error.")[0], "Provider hiccup")
        self.assertEqual(status.friendly_error("HTTP 401: unauthorized")[0], "Key rejected")
        self.assertEqual(status.friendly_error("DataForSEO 40104: verify your account")[0], "Needs verification")
