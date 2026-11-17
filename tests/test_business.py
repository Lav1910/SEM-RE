"""change 53: audits read what the business sells and ask about that, not just new-home building.
Profiles and answer text are invented test content. Run: python3 -m unittest discover tests"""
import os as _os, tempfile as _tf
_os.environ.setdefault("HBM_DATA_DIR", _tf.mkdtemp(prefix="hbm-test-")); _os.environ.setdefault("HBM_KV_DIR", _tf.mkdtemp(prefix="hbm-kv-test-"))
_os.environ.setdefault("HBM_PREWARM_PDFS", "0")
import json
import os
import sys
import time
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aeo import analyze, business, questions  # noqa: E402

MARKET = {"city": "Columbus", "state": "Ohio", "state_code": "OH"}


def remodeler():
    return {"name": "Russell Room Remodelers", "domain": "russellroom.com", "title": "Bathroom & Basement Remodeling in Columbus | Russell Room Remodelers",
            "meta_description": "Columbus bathroom remodeling and basement finishing.", "headings": ["Bathroom Remodeling", "Basement Finishing", "Kitchen Remodeling"],
            "nav_links": ["Bathrooms", "Basements", "Kitchens", "Contact"], "opening_text": "We remodel bathrooms and finish basements across Columbus. " * 5,
            "services": ["remodeling"], "areas_served": [], "word_count": 900, "schema_types": []}


def builder():
    return {"name": "Acme Custom Homes", "domain": "acmehomes.com", "title": "Acme Custom Homes | Custom Home Builder in Columbus",
            "meta_description": "Custom homes built on your lot.", "headings": ["Floor plans", "Communities"], "nav_links": ["Floor Plans"],
            "opening_text": "Acme builds custom homes and new construction homes in Columbus. " * 5,
            "services": ["custom homes", "build on your lot", "floor plans"], "areas_served": [], "word_count": 900, "schema_types": []}


class Detect(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(business.llm, "available", return_value=False)  # keyword rules only, no network
        p.start(); self.addCleanup(p.stop)

    def test_remodeler_reads_as_a_service_business(self):
        b = business.detect(remodeler())
        self.assertEqual(b["kind"], "service")
        names = [s["name"] for s in b["services"]]
        self.assertEqual(names[:2], ["Bathroom remodeling", "Basement finishing"])
        self.assertEqual((b["noun"], b["nouns"]), ("remodeler", "remodelers"))

    def test_builder_stays_a_builder(self):
        b = business.detect(builder())
        self.assertEqual(b["kind"], "home_builder")
        self.assertIn("Custom homes", [s["name"] for s in b["services"]])

    def test_unreadable_site_defaults_to_builder(self):
        self.assertEqual(business.detect({"word_count": 3, "site_blocked": 403})["kind"], "home_builder")

    def test_ai_answer_is_used_when_connected(self):
        ans = {"is_home_builder": False, "business": "roofing company", "noun": "roofer", "nouns": "roofers", "customers": "homeowners",
               "services": [{"name": "roof replacement", "pros": "roof replacement companies"}, {"name": "Roof repair", "pros": "roofers"}]}
        with mock.patch.object(business.llm, "available", return_value=True), mock.patch.object(business.llm, "json_answer", return_value=(ans, 0)):
            b = business.detect(remodeler())
        self.assertEqual(b["label"], "roofing company")
        self.assertEqual(b["services"][0]["name"], "Roof replacement")


class Questions(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(business.llm, "available", return_value=False)
        p.start(); self.addCleanup(p.stop)

    def test_remodeler_questions_are_about_its_services(self):
        P = remodeler(); P["business"] = business.detect(P)
        qs = questions.build(P, MARKET, 14)
        text = " ".join(q["prompt"] for q in qs).lower()
        self.assertIn("who are the best bathroom remodelers in columbus, oh?", text)
        self.assertIn("basement finishing", text)
        self.assertIn("how much does bathroom remodeling cost in columbus", text)
        self.assertNotIn("custom home", text)
        self.assertNotIn("home builder", text)
        self.assertIn("Is Russell Room Remodelers a good choice for bathroom remodeling?", [q["prompt"] for q in qs])
        self.assertEqual(len({q["id"] for q in qs}), len(qs))
        self.assertTrue(all(q["keyword"] == q["keyword"].lower() for q in qs))

    def test_builder_questions_are_unchanged(self):
        P = builder()
        old = questions.build(dict(P), MARKET, 14)
        P["business"] = business.detect(P)
        self.assertEqual(questions.build(P, MARKET, 14), old)

    def test_added_service_gets_a_question(self):
        P = remodeler(); P["business"] = business.apply_choice(business.detect(P), ["Bathroom remodeling", "Walk-in showers"])
        qs = questions.build(P, MARKET, 14)
        prompts = [q["prompt"] for q in qs]
        self.assertIn("Which companies are best for walk-in showers in Columbus, OH?", prompts)
        self.assertFalse([p for p in prompts if "basement" in p.lower()])  # unticked

    def test_builder_with_an_added_service(self):
        P = builder(); P["business"] = business.apply_choice(business.detect(P), ["Custom homes", "Home additions"])
        prompts = [q["prompt"] for q in questions.build(P, MARKET, 14)]
        self.assertIn("Which companies are best for home additions in Columbus, OH?", prompts)
        self.assertFalse([p for p in prompts if "own land" in p])  # "Build on your lot" was unticked

    def test_city_sets_follow_the_business(self):
        P = remodeler(); b = business.detect(P)
        loc = {"label": "Dublin, OH", "name": "Dublin", "city": "Dublin", "state": "Ohio", "state_code": "OH", "kind": "city"}
        qs = questions.for_location(loc, 3, b)
        self.assertEqual(qs[0]["prompt"], "Who are the best bathroom remodelers in Dublin, OH?")
        self.assertEqual(questions.city_keyword(b).format(kw="dublin oh"), "bathroom remodelers dublin oh")
        self.assertIn("custom home builders", questions.for_location(loc, 1)[0]["keyword"])


class Names(unittest.TestCase):
    def test_remodeler_competitors_are_found(self):
        P = remodeler(); P["business"] = {"kind": "service", "label": "home remodeling company", "noun": "remodeler", "nouns": "remodelers",
                                         "customers": "homeowners", "services": [{"name": "Bathroom remodeling", "pros": "bathroom remodelers"}]}
        qs = [{"id": "q1", "prompt": "Who are the best bathroom remodelers in Columbus, OH?", "keyword": "x", "kind": "discovery", "weight": 10}]
        ans = [{"engine": "chatgpt", "question_id": "q1", "status": "ok", "citations": [], "local_pack": [], "organic": [],
                "text": "Top picks: Apex Kitchen & Bath, Smith Remodeling and Buckeye Basements. Also Bathroom Remodeling experts. "
                        "Russell Room Remodelers is also well reviewed."}]
        X = analyze.analyze(P, MARKET, qs, ans, [], None)
        names = [b["name"] for b in X["leaderboard"] if not b.get("prospect")]
        self.assertEqual(sorted(names), ["Apex Kitchen & Bath", "Buckeye Basements", "Smith Remodeling"])
        self.assertTrue(X["cells"][0]["mentioned"])

    def test_words_for_old_audits(self):
        W = business.words({"profile": {"name": "x"}})
        self.assertEqual((W["noun"], W["customers"], W["is_builder"]), ("builder", "home buyers", True))


class Report(unittest.TestCase):
    def test_service_report_talks_about_the_trade(self):
        path = os.environ.get("HBM_SAMPLE_AUDIT")
        if not path or not os.path.exists(path):
            self.skipTest("no sample audit")
        from aeo import render
        A = json.load(open(path))
        A["profile"]["business"] = {"kind": "service", "label": "home remodeling company", "noun": "remodeler", "nouns": "remodelers",
                                    "customers": "homeowners", "services": [{"name": "Bathroom remodeling", "pros": "bathroom remodelers"},
                                                                            {"name": "Basement finishing", "pros": None}]}
        html = render.render(A)
        self.assertIn("ask AI tools about bathroom remodeling and basement finishing", html)
        self.assertIn("What AI tells local homeowners", html)
        self.assertNotIn("home buyer", html.lower())
        self.assertNotIn("{noun}", html)
        del A["profile"]["business"]
        self.assertIn("What AI tells local home buyers", render.render(A))


class Presets(unittest.TestCase):
    """change 56: HBM is measured from San Diego plus the US home-building hubs."""
    def test_hbm_gets_san_diego_and_the_hubs(self):
        from aeo import locations
        P = {"domain": "homebuildermarketers.com", "areas_served": [], "location_links": [], "area_phrases": [], "sitemap_urls": []}
        M = {"city": "San Diego", "state": "California", "state_code": "CA"}
        with mock.patch.object(locations, "validate", lambda c, m, provider="gemini": [
                {"label": "San Diego, CA", "name": "San Diego", "city": "San Diego", "state": "California", "state_code": "CA", "kind": "city", "main": True, "sources": ["main market"]}]):
            found = locations.find(P, M)
        labels = [l["label"] for l in found]
        self.assertEqual(labels[0], "San Diego, CA")
        self.assertIn("Dallas, TX", labels); self.assertIn("Denver, CO", labels)
        self.assertEqual(len(labels), 13)
        self.assertIsNone(locations.preset_for("someremodeler.com"))

    def test_niche_rule_is_in_the_prompt(self):
        self.assertIn("SEO for home builders", business.PROMPT)


class Dashboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from webapp import app as appmod, db
        cls.app, cls.db = appmod.app, db
        appmod.app.config["TESTING"] = True
        db.init()
        cls.uid = db.x("INSERT INTO users (email, name, password_hash, role, active, created_at) VALUES (?,?,?,?,1,?)",
                       (f"svc{uuid.uuid4().hex[:6]}@homebuildermarketers.com", "Svc", "x", "sales", time.time()))

    def client(self):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s["uid"] = self.uid; s["csrf"] = "t"
        return c

    def test_picked_services_are_saved_with_the_audit(self):
        dom = f"svc{uuid.uuid4().hex[:6]}.com"
        with mock.patch("webapp.jobs.kick"):
            self.client().post("/audits", data={"csrf": "t", "url": dom, "svc_custom": "1", "svc": ["Bathroom remodeling", "Walk-in showers"]})
            self.client().post("/audits", data={"csrf": "t", "url": "auto" + dom, "svc": ["Bathroom remodeling"]})  # untouched: read from the site
        row = self.db.q("SELECT services FROM audits WHERE domain=?", (dom,), one=True)
        self.assertEqual(json.loads(row["services"]), ["Bathroom remodeling", "Walk-in showers"])
        self.assertIsNone(self.db.q("SELECT services FROM audits WHERE domain=?", ("auto" + dom,), one=True)["services"])
        self.db.x("DELETE FROM audits WHERE domain IN (?, ?)", (dom, "auto" + dom))  # don't leave queued work for the job tests

    def test_dashboard_has_a_services_picker(self):
        html = self.client().get("/").get_data(as_text=True)
        self.assertIn("svc-dd", html)
        self.assertIn('<optgroup label="Canada">', html)
        self.assertIn("Business website", html)

    def test_wordmark_everywhere(self):
        html = self.client().get("/").get_data(as_text=True)
        self.assertIn('class="wm-t"', html)
        self.assertEqual(html.count("hbm-logo.png"), 1)  # the HBM logo stays in the far-left rail only
        login = self.app.test_client().get("/login").get_data(as_text=True)
        self.assertIn("wm-light", login); self.assertNotIn("hbm-logo.png", login); self.assertIn("Syncopate", login)

    def test_old_address_redirects_to_the_new_one(self):
        from webapp import app as appmod
        c = self.app.test_client()
        with mock.patch.object(appmod, "PUBLIC_URL", "https://aeosnapshot.homebuildermarketers.com"):
            r = c.get("/c/abc123?x=1", base_url="https://hbm-ai-visibility.vercel.app")
            self.assertEqual(r.status_code, 308)
            self.assertEqual(r.headers["Location"], "https://aeosnapshot.homebuildermarketers.com/c/abc123?x=1")
            self.assertEqual(c.get("/login", base_url="https://hbm-ai-visibility.vercel.app").headers["Location"],
                             "https://aeosnapshot.homebuildermarketers.com/login")
            self.assertNotEqual(c.post("/mcp", base_url="https://hbm-ai-visibility.vercel.app", json={}).status_code, 308)  # agents keep working
            self.assertNotEqual(c.get("/.well-known/oauth-protected-resource", base_url="https://hbm-ai-visibility.vercel.app").status_code, 308)
            self.assertEqual(c.get("/login", base_url="https://aeosnapshot.homebuildermarketers.com").status_code, 200)
            self.assertEqual(c.get("/login", base_url="https://hbm-ai-visibility-git-x.vercel.app").status_code, 200)  # previews untouched

    def test_favicon(self):
        r = self.app.test_client().get("/favicon.ico")
        self.assertEqual(r.status_code, 200)
        self.assertIn('rel="icon"', self.app.test_client().get("/login").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()


class Canada(unittest.TestCase):
    """change 58: Canadian businesses audit exactly like US ones, with Canadian locations and search data."""
    def test_provinces_are_markets(self):
        from aeo import engines, site
        m = site._validated("vancouver", "BC", "website markup")
        self.assertEqual((m["city"], m["state"], m["state_code"], m["country"]), ("Vancouver", "British Columbia", "BC", "CA"))
        self.assertEqual(site._validated("Austin", "Texas", "x")["country"], "US")
        self.assertEqual(engines.country_of({"state_code": "ON"}), "CA")
        self.assertEqual(engines.country_of({"state_code": "CA"}), "US")  # California, not Canada
        self.assertEqual(engines._dfs_location(m), "Vancouver,British Columbia,Canada")

    def test_canadian_places_and_questions(self):
        from aeo import locations, semrush
        loc = locations.typed("Burnaby, BC")
        self.assertEqual((loc["state"], locations.market_for(loc)["country"]), ("British Columbia", "CA"))
        self.assertEqual(semrush.database({"state_code": "BC"}), "ca")
        P = {"name": "Infinity West", "services": ["custom homes"], "areas_served": []}
        qs = questions.build(P, {"city": "Vancouver", "state": "British Columbia", "state_code": "BC"}, 14)
        self.assertEqual(qs[0]["prompt"], "Who are the best custom home builders in Vancouver, BC?")

    def test_name_from_the_page_beats_a_template_name(self):
        from aeo import site
        self.assertEqual(site.name_in_text("About Infinity West Development Group, Vancouver", "infinitywestdevelopment.com"),
                         "Infinity West Development Group")
        self.assertEqual(site.name_in_text("© 2026 Infinity West. All rights reserved", "infinitywestdevelopment.com"), "Infinity West")
        self.assertIsNone(site.name_in_text("Nothing related here", "acme.com"))


class CapturedAnswers(unittest.TestCase):
    """change 59: ChatGPT and Gemini as the consumer apps answer, via DataForSEO, with the official APIs as backup."""
    M = {"city": "Columbus", "state": "Ohio", "state_code": "OH", "country": "US"}
    Q = {"id": "q1", "prompt": "Who are the best bathroom remodelers in Columbus, OH?", "keyword": "x", "kind": "discovery"}
    RESP = {"tasks": [{"status_code": 20000, "cost": 0.004, "result": [{"model": "gpt-6", "markdown": "Try **Creative Bath Renovations**.",
            "sources": [{"url": "https://www.houzz.com/x", "title": "Houzz"}],
            "items": [{"type": "chat_gpt_local_businesses", "items": [{"title": "Shower King LLC", "url": "https://showerking.com", "rating": {"value": 4.9}}]}]}]}]}

    def setUp(self):
        from aeo import engines, kv
        self.e = engines
        kv.update("answers", lambda d: {"captured": True}, {})
        p = mock.patch.object(engines, "_dfs_ready", return_value=True); p.start(); self.addCleanup(p.stop)

    def test_captured_answer(self):
        with mock.patch.object(self.e, "_post", return_value=self.RESP) as post:
            r = self.e._ask("chatgpt", self.Q, self.M)
        body = post.call_args[0][1][0]
        self.assertEqual((body["location_name"], body["force_web_search"]), ("United States", True))
        self.assertEqual((r["status"], r["source"], r["cost_usd"], r["model"]), ("ok", "dataforseo", 0.004, "gpt-6"))
        self.assertEqual(r["cards"][0]["title"], "Shower King LLC")
        self.assertEqual(r["citations"][0]["domain"], "houzz.com")

    def test_gemini_asks_from_the_city(self):
        self.assertEqual(self.e._captured_locations("gemini", self.M)[0], "Columbus, OH,Ohio,United States")
        self.assertEqual(self.e._captured_locations("gemini", {"city": "Vancouver", "state": "British Columbia", "state_code": "BC"})[0],
                         "Vancouver,British Columbia,Canada")
        self.assertEqual(self.e._captured_locations("chatgpt", self.M), ["United States"])

    def test_falls_back_to_the_official_api(self):
        api = lambda q, m: self.e._record("chatgpt", q, text="From the API", cost_usd=None)
        with mock.patch.object(self.e, "ask_captured", side_effect=RuntimeError("DataForSEO 50000: down")), \
                mock.patch.dict(self.e.ASK, {"chatgpt": api}), mock.patch.object(self.e.config, "key", return_value="k"):
            r = self.e._ask("chatgpt", self.Q, self.M)
        self.assertEqual((r["status"], r["text"], r["source"]), ("ok", "From the API", "api (fallback)"))
        self.assertIn("down", r["dfs_error"])

    def test_admin_can_switch_it_off(self):
        from aeo import kv
        kv.update("answers", lambda d: {"captured": False}, {})
        api = lambda q, m: self.e._record("chatgpt", q, text="API")
        with mock.patch.dict(self.e.ASK, {"chatgpt": api}), mock.patch.object(self.e, "ask_captured") as cap, \
                mock.patch.object(self.e.config, "key", return_value="k"):
            r = self.e._ask("chatgpt", self.Q, self.M)
        cap.assert_not_called()
        self.assertEqual(r["text"], "API")

    def test_cards_count_as_named_and_as_competitors(self):
        P = {"name": "Shower King", "domain": "showerking.com", "areas_served": [], "services": []}
        qs = [{"id": "q1", "prompt": "Who are the best bathroom remodelers in Columbus, OH?", "keyword": "x", "kind": "discovery", "weight": 10}]
        ans = [{"engine": "chatgpt", "question_id": "q1", "status": "ok", "citations": [], "local_pack": [], "organic": [],
                "text": "Try Creative Bath Renovations.", "cards": [{"title": "Shower King LLC", "domain": "showerking.com"},
                                                                    {"title": "Re-Bath of Columbus", "domain": "rebath.com"}]}]
        X = analyze.analyze(P, self.M, qs, ans, [], None)
        self.assertTrue(X["cells"][0]["mentioned"])  # only in a card, not in the text
        self.assertIn("Re-Bath of Columbus", X["cells"][0]["competitors"])


class Names59(unittest.TestCase):
    def test_descriptive_phrases_are_not_businesses(self):
        for n in ["Expert Austin Custom Home Builders", "Full-Service Design-Build", "Highly Rated Local Contractors",
                  "Specialized & Highly Rated Mid-Sized Remodelers", "Exceptional New Construction"]:
            self.assertTrue(analyze.descriptive(n, ["austin"]), n)
        for n in ["Katz Builders", "Mosier Luxury Homes", "Creative Bath Renovations", "Dave Fox Design-Build"]:
            self.assertFalse(analyze.descriptive(n, ["austin"]), n)
        self.assertTrue(analyze.NOT_A_BUSINESS.search("Housing Public Registry of Residential Builders"))

    def test_variants_merge_and_suffixes_go(self):
        self.assertEqual(analyze.norm_name("J.S. Brown & Co."), "J.S. Brown")
        self.assertEqual(analyze.norm_name("Mik Homes - Kitchen and Bath Remodeling"), "Mik Homes")
        self.assertEqual(analyze.dedupe_names(["Creative Bath", "Creative Bath Renovations", "JDL Homes", "JDL Homes Vancouver", "Katz Builders"]),
                         ["Creative Bath Renovations", "JDL Homes Vancouver", "Katz Builders"])
        self.assertTrue(analyze.same_business("Dave Fox Design Build", "Dave Fox Design-Build"))
        self.assertFalse(analyze.same_business("Katz Builders", "Kerr Design Build"))


class Australia(unittest.TestCase):
    """change 60: Australian businesses audit like US and Canadian ones; shared codes (WA, NT) follow the country."""
    def test_states_and_shared_codes(self):
        from aeo import engines, site
        self.assertEqual(site._validated("perth", "WA", "x", "AU")["state"], "Western Australia")
        self.assertEqual(site._validated("seattle", "WA", "x")["state"], "Washington")
        self.assertEqual(site._validated("darwin", "NT", "x", "AU")["country"], "AU")
        self.assertEqual(site._validated("Sydney", "NSW", "x")["country"], "AU")  # NSW is only Australian
        self.assertEqual(engines.country_of({"state": "Western Australia", "state_code": "WA"}), "AU")
        self.assertEqual(engines._dfs_location({"city": "Sydney", "state": "New South Wales", "state_code": "NSW"}), "Sydney,New South Wales,Australia")
        self.assertEqual(engines._captured_locations("gemini", {"city": "Sydney", "state": "New South Wales", "state_code": "NSW"})[0],
                         "Sydney,New South Wales,Australia")

    def test_country_hints_and_phones(self):
        from aeo import site
        self.assertEqual(site.country_hint("mojohomes.com.au"), "AU")
        self.assertEqual(site.country_hint("x.com", {"addressCountry": "AU"}), "AU")
        self.assertIsNone(site.country_hint("x.com"))
        self.assertEqual(site.phone_numbers("(02) 9876 5432, +61 412 345 678, 1300 123 456, (512) 555-0101"),
                         ["0298765432", "0412345678", "1300123456", "5125550101"])
        self.assertEqual(site.format_phone("0298765432"), "02 9876 5432")

    def test_places_and_search_data(self):
        from aeo import locations, semrush, engines
        self.assertEqual(locations.typed("Fremantle, WA", "entered", "AU")["state"], "Western Australia")
        self.assertEqual(locations.typed("Spokane, WA")["state"], "Washington")
        self.assertEqual(semrush.database({"state": "Victoria", "state_code": "VIC"}), "au")
        self.assertEqual(engines.COUNTRY_ADJ[engines.country_of({"state_code": "QLD"})], "Australian")

    def test_error_pages_are_not_names(self):
        from aeo import site
        self.assertTrue(site.ERROR_PAGE.search("ERROR: The request could not be satisfied"))
        self.assertFalse(site.ERROR_PAGE.search("Mojo Homes | Custom Home Builders Sydney"))
