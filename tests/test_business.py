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
        self.assertIn("Business website", html)

    def test_favicon(self):
        r = self.app.test_client().get("/favicon.ico")
        self.assertEqual(r.status_code, 200)
        self.assertIn('rel="icon"', self.app.test_client().get("/login").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
