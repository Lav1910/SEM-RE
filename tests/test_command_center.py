"""Change 64: SEO Delivery Command Center: owed vs delivered, health scoring, access, read-only connectors, sync jobs."""
import os as _os, tempfile as _tf
_os.environ.setdefault("HBM_KV_DIR", _tf.mkdtemp(prefix="hbm-kv-test-"))
import datetime
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("HBM_DATA_DIR", tempfile.mkdtemp(prefix="hbm-test-"))

from webapp.cc import core, health, ledger  # noqa: E402
from webapp.cc.sources import clickup, wordpress  # noqa: E402

D = datetime.date


def scope(item, qty, start, end=None, at=1.0):
    return {"item": item, "qty": qty, "start_month": start, "end_month": end, "set_at": at}


def dl(item, month, status="published", verified=False, not_ours=0, merged=None, source="wp", id_=1):
    return {"id": id_, "item": item, "month": month, "status": status, "verified_at": 1.0 if verified else None,
            "not_ours": not_ours, "merged_into": merged, "source": source}


FULL = [scope("blog", 4, "2026-01"), scope("gbp_post", 8, "2026-01"), scope("gbp_photo", 0, "2026-01"),
        scope("review_reply", 0, "2026-01"), scope("report", 1, "2026-01")]


class Ledger(unittest.TestCase):
    def test_scope_versions_and_missing(self):
        rows = [scope("blog", 2, "2026-01", "2026-08"), scope("blog", 4, "2026-09", at=2.0)]
        self.assertEqual(ledger.scope_qty(rows, "blog", "2026-03"), 2)
        self.assertEqual(ledger.scope_qty(rows, "blog", "2026-10"), 4)
        self.assertIsNone(ledger.scope_qty(rows, "blog", "2025-12"))  # before any scope: missing, never zero
        self.assertIsNone(ledger.scope_qty(rows, "gbp_post", "2026-10"))

    def test_only_verified_blogs_count(self):
        rows = [dl("blog", "2026-10", verified=True, id_=1), dl("blog", "2026-10", id_=2),  # closed, not live-checked
                dl("blog", "2026-10", verified=True, not_ours=1, id_=3), dl("blog", "2026-10", verified=True, merged=1, id_=4)]
        c = ledger.cell("2026-02-01", FULL, rows, "blog", "2026-10", D(2026, 11, 19))
        self.assertEqual((c["counted"], c["reported"], c["owed"], c["state"]), (1, 1, 4, "short"))

    def test_report_counts_when_am_confirms_and_is_late_after_the_7th(self):
        sent = [dl("report", "2026-11", source="clickup")]
        self.assertEqual(ledger.cell(None, FULL, sent, "report", "2026-11", D(2026, 11, 19))["state"], "done")
        self.assertEqual(ledger.cell(None, FULL, [], "report", "2026-11", D(2026, 11, 6))["state"], "open")
        self.assertEqual(ledger.cell(None, FULL, [], "report", "2026-11", D(2026, 11, 8))["state"], "late")

    def test_gbp_marked_done_is_unverified_not_green(self):
        rows = [dl("gbp_post", "2026-10", source="clickup", id_=i) for i in range(8)]
        c = ledger.cell(None, FULL, rows, "gbp_post", "2026-10", D(2026, 11, 19))
        self.assertEqual(c["state"], "unverified")
        self.assertEqual(ledger.cell(None, FULL, rows[:3], "gbp_post", "2026-10", D(2026, 11, 19))["state"], "short")

    def test_go_live_gating_and_states(self):
        t = D(2026, 11, 19)
        self.assertEqual(ledger.cell("2026-09-06", FULL, [], "blog", "2026-09", t)["state"], "pre_launch")  # launch month
        self.assertEqual(ledger.cell("2026-09-06", FULL, [], "blog", "2026-10", t)["state"], "short")
        self.assertEqual(ledger.cell("2026-09-06", FULL, [], "blog", "2026-11", t)["state"], "open")
        self.assertEqual(ledger.cell("2026-09-06", FULL, [], "gbp_photo", "2026-10", t)["state"], "not_owed")
        self.assertEqual(ledger.cell("2026-09-06", [], [], "blog", "2026-10", t)["state"], "scope_missing")
        months, grid = ledger.year_grid("2026-09-06", FULL, [], t)
        self.assertEqual((months[0], months[-1], len(grid["blog"])), ("2025-12", "2026-11", 12))

    def test_short_run_counts_months_in_a_row(self):
        run = ledger.short_run("2026-09-06", FULL, [], "blog", D(2026, 12, 3))
        self.assertEqual([m for m, _, _ in run], ["2026-11", "2026-10"])


def ctx(**kw):
    base = {"client": {"id": 1, "name": "Test Homes", "domain": "test.example", "package": "Organic Max", "status": "active",
                       "am_id": 1, "seo_id": 2, "dev_id": None, "writer_id": None, "go_live": "2026-09-06"},
            "scope": FULL, "deliverables": [], "tasks": [], "metrics": {},
            "sources": {k: {"state": "ok", "last_ok": time.time()} for k, _, _ in core.SOURCES},
            "today": D(2026, 12, 3), "now": time.time()}
    base.update(kw)
    return base


def task(item, status="to do", status_type="open", due=None, updated=None, assignees='[{"id":1,"name":"A"}]', tid="t1", name="Task"):
    return {"task_id": tid, "item": item, "status": status, "status_type": status_type, "due": due, "recurring": None,
            "date_updated": updated, "date_created": updated, "assignees": assignees, "name": name}


def codes(flags):
    return {f["code"] for f in flags}


class Health(unittest.TestCase):
    def ts(self, d):
        return datetime.datetime(d.year, d.month, d.day, 12, tzinfo=core.TZ).timestamp()

    def covered(self, day=D(2026, 12, 20)):
        return [task(i, due=self.ts(day), tid=f"r-{i}") for i in ("blog", "gbp_post", "report")]

    def test_marcello_pattern_is_red_with_reasons(self):
        # scope written, launched in September, nothing delivered, no recurring tasks
        f = health.evaluate(ctx())
        self.assertEqual(health.colour(f), "red")
        self.assertTrue({"short", "no_task"} <= codes(f))
        short = next(x for x in f if x["fkey"] == "short:blog")
        self.assertIn("0 of 4 delivered in Nov", short["reason"])
        self.assertIn("also short in Oct", short["reason"])
        self.assertEqual(short["owner"], "seo")

    def test_blank_scope_and_owners_are_red(self):
        c = ctx(scope=[])["client"]
        f = health.evaluate(ctx(scope=[], client={**c, "seo_id": None, "domain": None}))
        self.assertTrue({"scope_missing", "owner_missing", "domain_missing"} <= codes(f))
        self.assertEqual(health.colour(f), "red")

    def test_green_only_when_everything_is_on_track_and_fresh(self):
        good = [dl("blog", "2026-11", verified=True, id_=i) for i in range(4)] + \
               [dl("gbp_post", "2026-11", verified=True, source="gbp", id_=10 + i) for i in range(8)] + [dl("report", "2026-12", source="clickup", id_=50), dl("report", "2026-11", source="clickup", id_=51)]
        f = health.evaluate(ctx(deliverables=good, tasks=self.covered()))
        self.assertEqual(f, [])
        self.assertEqual(health.colour(f), "green")
        # the same client with Search Console not connected is amber, never green
        srcs = {k: {"state": "ok", "last_ok": time.time()} for k in ("clickup", "wordpress", "gbp")}
        f = health.evaluate(ctx(deliverables=good, tasks=self.covered(), sources=srcs))
        self.assertEqual(health.colour(f), "amber")
        self.assertIn("source_off", codes(f))
        # and stale data (over 48 hours) is amber too
        old = {k: {"state": "ok", "last_ok": time.time() - 50 * 3600} for k, _, _ in core.SOURCES}
        self.assertIn("stale", codes(health.evaluate(ctx(deliverables=good, tasks=self.covered(), sources=old))))

    def test_approval_waiting_more_than_five_working_days(self):
        today = D(2026, 12, 3)  # Thursday
        t_ok = task("blog", "awaiting approval", updated=self.ts(D(2026, 11, 27)), tid="a1")  # 4 working days
        t_old = task("blog", "awaiting approval", updated=self.ts(D(2026, 11, 24)), tid="a2")  # 7 working days
        f = health.evaluate(ctx(tasks=self.covered() + [t_ok, t_old], today=today))
        stuck = [x for x in f if x["code"] == "approval"]
        self.assertEqual([x["fkey"] for x in stuck], ["approval:a2"])
        self.assertEqual(stuck[0]["severity"], "amber")

    def test_client_input_and_unassigned_and_overdue(self):
        t1 = task("gbp_photo", "waiting on client", updated=self.ts(D(2026, 11, 20)), tid="c1")
        t2 = task("blog", "to do", due=self.ts(D(2026, 11, 28)), assignees="[]", tid="o1")
        f = health.evaluate(ctx(tasks=self.covered() + [t1, t2]))
        self.assertTrue({"client_input", "unassigned", "overdue_tasks"} <= codes(f))

    def test_site_down_needs_two_failures(self):
        m = {("wordpress", "home_fails", "latest"): {"value": 1}, ("wordpress", "home_status", "latest"): {"value": 503}}
        self.assertNotIn("site_down", codes(health.evaluate(ctx(metrics=m))))
        m[("wordpress", "home_fails", "latest")] = {"value": 2}
        self.assertIn("site_down", codes(health.evaluate(ctx(metrics=m))))

    def test_paused_clients_raise_nothing(self):
        c = ctx()["client"]
        self.assertEqual(health.evaluate(ctx(client={**c, "status": "paused"})), [])
        self.assertEqual(health.colour([], "paused"), "paused")

    def test_escalation_by_working_days(self):
        thu = D(2026, 12, 3)
        self.assertEqual(health.escalation(self.ts(D(2026, 12, 2)), thu)[0], "new")
        self.assertEqual(health.escalation(self.ts(D(2026, 12, 1)), thu)[0], "owner")
        self.assertEqual(health.escalation(self.ts(D(2026, 11, 26)), thu), ("you", 5))
        self.assertEqual(core.working_days_between(D(2026, 11, 27), D(2026, 11, 30)), 1)  # Fri -> Mon


class ClickUpReading(unittest.TestCase):
    def test_classify_and_status(self):
        self.assertEqual(clickup.classify("November Blog #2 – deck permits"), "blog")
        self.assertEqual(clickup.classify("GBP Post: fall promo"), "gbp_post")
        self.assertEqual(clickup.classify("Upload GBP photos"), "gbp_photo")
        self.assertEqual(clickup.classify("Manage & respond to reviews"), "review_reply")
        self.assertEqual(clickup.classify("Weekly posts (GMB)-Hammell"), "gbp_post")
        self.assertEqual(clickup.classify("Bi-weekly GBP posting"), "gbp_post")
        self.assertEqual(clickup.classify("Weekly GMB"), "gbp_post")
        self.assertEqual(clickup.classify("Upload to GBP"), "gbp_photo")
        self.assertIsNone(clickup.classify("GMB optimizations"))  # one-off setup, not an owed monthly item
        self.assertIsNone(clickup.classify("Remove review gating"))
        self.assertIsNone(clickup.classify("Content review (SEO team)"))
        self.assertEqual(clickup.classify("Review replies (Oct)"), "review_reply")
        self.assertEqual(clickup.classify("Monthly SEO report"), "report")
        self.assertEqual(clickup.classify("Task 1", "Blogs"), "blog")
        self.assertEqual(clickup.map_status("Awaiting Client Approval"), "awaiting_approval")
        self.assertEqual(clickup.map_status("approved"), "approved")
        self.assertEqual(clickup.map_status("in progress"), "drafted")
        self.assertEqual(clickup.map_status("whatever", "closed"), "published")
        self.assertEqual(core.clean_name("JEP‎ Contracting"), "JEP Contracting")
        self.assertEqual(core.root_domain("https://www.Example.com/about?x=1"), "example.com")

    def test_only_get_requests_and_token_never_in_errors(self):
        sent = []

        class Resp:
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(self.body).encode()

        def fake_open(req, timeout):
            sent.append(req)
            return Resp({"teams": [{"id": "1"}], "spaces": [{"id": "s1", "name": "Growth Package"}],
                         "folders": [{"id": "f1", "name": "JEP‎ Contracting"}], "lists": [], "tasks": []})
        with mock.patch.object(clickup, "token", return_value="pk_test_secret"), mock.patch.object(clickup, "_open", fake_open), \
                mock.patch.object(clickup, "MIN_GAP", 0):
            folders = clickup.folders_for_import()
            clickup.folder_tasks("f1")
        self.assertTrue(sent)
        self.assertEqual({r.get_method() for r in sent}, {"GET"})
        self.assertEqual(folders[0]["name"], "JEP Contracting")
        self.assertEqual(folders[0]["package"], "Growth")

    def test_no_token_means_not_connected(self):
        with mock.patch.object(clickup, "token", return_value=None):
            with self.assertRaises(clickup.NotConnected):
                clickup.get("/team")


class WithApp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from webapp import app as appmod, db
        cls.app, cls.db = appmod.app, db
        appmod.app.config["TESTING"] = True
        db.init()
        for email, adm in (("ccboss@homebuildermarketers.com", 1), ("ccadmin2@homebuildermarketers.com", 1), ("ccrep@homebuildermarketers.com", 0)):
            if not db.q("SELECT 1 FROM users WHERE email=?", (email,), one=True):
                db.x("INSERT INTO users (email, name, password_hash, is_admin, role, created_at) VALUES (?,?,?,?,?,?)",
                     (email, email.split("@")[0], "!", adm, "admin" if adm else "sales", time.time()))
        cls.ids = {r["email"].split("@")[0]: r["id"] for r in db.q("SELECT id, email FROM users WHERE email LIKE 'cc%'")}

    def client(self, who=None):
        c = self.app.test_client()
        if who:
            with c.session_transaction() as s:
                s["uid"], s["csrf"] = self.ids[who], "t"
        return c

    def env(self, on=True):
        return mock.patch.dict(os.environ, {"HBM_COMMAND_CENTER": "1" if on else "", "COMMAND_CENTER_EMAILS": "ccboss@homebuildermarketers.com"})

    def new_client(self, **kw):
        from webapp.cc import data
        cid = self.db.x("INSERT INTO cc_clients (name, domain, status, go_live, created_at) VALUES (?,?, 'active', ?, ?)",
                        (kw.get("name", "Test Builders"), kw.get("domain", "test-builders.example"), kw.get("go_live"), time.time()))
        data.recompute(cid)
        return cid


class Access(WithApp):
    def test_only_listed_admins_see_it_everyone_else_gets_404(self):
        with self.env(on=False):
            self.assertEqual(self.client("ccboss").get("/delivery").status_code, 404)  # feature flag off
        with self.env():
            self.assertEqual(self.client("ccboss").get("/delivery").status_code, 200)
            self.assertEqual(self.client("ccadmin2").get("/delivery").status_code, 404)  # admin, not granted
            self.assertEqual(self.client("ccrep").get("/delivery").status_code, 404)  # sales
            self.assertEqual(self.client().get("/delivery").status_code, 404)  # signed out
            self.assertEqual(self.client("ccrep").post("/delivery/sync", data={"csrf": "t"}).status_code, 404)
            self.assertNotIn(b"Command Center", self.client("ccadmin2").get("/").data)
            self.assertIn(b"SEO Delivery Command Center", self.client("ccboss").get("/").data)
            # granting access works for admins only
            r = self.client("ccboss").post("/delivery/people", data={"csrf": "t", "action": "grant", "user_id": self.ids["ccadmin2"]})
            self.assertEqual(r.status_code, 302)
            self.assertEqual(self.client("ccadmin2").get("/delivery").status_code, 200)
            self.client("ccboss").post("/delivery/people", data={"csrf": "t", "action": "grant", "user_id": self.ids["ccrep"]})
            self.assertEqual(self.client("ccrep").get("/delivery").status_code, 404)
            self.client("ccboss").post("/delivery/people", data={"csrf": "t", "action": "revoke", "user_id": self.ids["ccadmin2"]})
            self.assertEqual(self.client("ccadmin2").get("/delivery").status_code, 404)

    def test_pages_render_and_scope_is_versioned_and_logged(self):
        with self.env():
            cid = self.new_client(go_live="2026-09-06")
            c = self.client("ccboss")
            for path in ("/delivery", f"/delivery/clients/{cid}", "/delivery/scope", "/delivery/import", "/delivery/people", "/delivery/sync"):
                self.assertEqual(c.get(path).status_code, 200, path)
            self.assertIn(b"Scope missing", c.get("/delivery").data)
            row = self.db.q("SELECT health FROM cc_clients WHERE id=?", (cid,), one=True)
            self.assertEqual(row["health"], "red")
            form = {"csrf": "t", "blog": "4", "gbp_post": "8", "gbp_photo": "0", "review_reply": "", "report": "1"}
            c.post(f"/delivery/clients/{cid}/scope", data=form)
            rows = self.db.q("SELECT * FROM cc_scope WHERE client_id=? ORDER BY item", (cid,))
            self.assertEqual({r["item"]: (r["qty"], r["start_month"]) for r in rows}["blog"], (4, "2026-09"))  # first scope: from go-live
            self.assertNotIn("review_reply", {r["item"] for r in rows})  # blank stays missing
            from webapp.cc import views
            with mock.patch.object(views, "today", return_value=D(2027, 1, 10)):
                views.set_scope(cid, "blog", 6, "ccboss@homebuildermarketers.com")
            rows = self.db.q("SELECT * FROM cc_scope WHERE client_id=? AND item='blog' ORDER BY start_month", (cid,))
            self.assertEqual([(r["qty"], r["start_month"], r["end_month"]) for r in rows], [(4, "2026-09", "2026-12"), (6, "2027-01", None)])
            hist = self.db.q("SELECT field, old, new FROM cc_history WHERE client_id=? AND entity='scope'", (cid,))
            self.assertTrue(any(h["field"] == "Blogs" and h["new"] == "4" for h in hist))
            flags = self.db.q("SELECT code FROM cc_flags WHERE client_id=? AND resolved_at IS NULL", (cid,))
            self.assertIn("scope_missing", {f["code"] for f in flags})  # review replies still blank


class Syncing(WithApp):
    def test_sitemap_baseline_never_counts_old_posts(self):
        from webapp.cc import sync
        cid = self.new_client(domain="no-wp.example")
        c = dict(self.db.q("SELECT * FROM cc_clients WHERE id=?", (cid,), one=True))
        live = mock.patch.object(wordpress, "check_live", return_value={"ok": True, "status": 200, "note": "200, indexable"})
        with mock.patch.object(wordpress, "home_status", return_value=200), live, \
                mock.patch.object(wordpress, "posts", side_effect=wordpress.NotWordPress("no feed")), \
                mock.patch.object(wordpress, "sitemap_post_urls", return_value=["https://no-wp.example/blog/a/", "https://no-wp.example/blog/b/"]):
            sync.sync_wordpress(c, [0])
        n = self.db.q("SELECT COUNT(*) AS n FROM cc_deliverables WHERE client_id=? AND month!='baseline'", (cid,), one=True)["n"]
        self.assertEqual(n, 0)
        with mock.patch.object(wordpress, "home_status", return_value=200), live, \
                mock.patch.object(wordpress, "posts", side_effect=wordpress.NotWordPress("no feed")), \
                mock.patch.object(wordpress, "sitemap_post_urls", return_value=["https://no-wp.example/blog/a/", "https://no-wp.example/blog/b/",
                                                                                "https://no-wp.example/blog/new-post/"]):
            sync.sync_wordpress(c, [0])
        rows = self.db.q("SELECT * FROM cc_deliverables WHERE client_id=? AND month!='baseline'", (cid,))
        self.assertEqual([r["url"] for r in rows], ["https://no-wp.example/blog/new-post/"])
        self.assertTrue(rows[0]["verified_at"])
        src = self.db.q("SELECT state FROM cc_sources WHERE client_id=? AND source='wordpress'", (cid,), one=True)
        self.assertEqual(src["state"], "partial")

    def test_wordpress_posts_are_live_checked_and_matched_to_tasks(self):
        from webapp.cc import sync
        cid = self.new_client(domain="wp.example")
        c = dict(self.db.q("SELECT * FROM cc_clients WHERE id=?", (cid,), one=True))
        month = core.month_of(core.today())
        posts = [{"id": "11", "date": month + "-05T10:00:00", "url": "https://wp.example/deck-permits-guide/", "title": "Deck permits guide for Dallas"},
                 {"id": "12", "date": month + "-06T10:00:00", "url": "https://wp.example/hidden/", "title": "Hidden"}]
        checks = {"https://wp.example/deck-permits-guide/": {"ok": True, "status": 200, "note": "ok"},
                  "https://wp.example/hidden/": {"ok": False, "status": 200, "note": "noindex"}}
        self.db.x("""INSERT INTO cc_deliverables (client_id, item, month, title, status, source, ext_id, task_id, updated_at)
                     VALUES (?, 'blog', ?, 'Blog: Deck permits guide for Dallas', 'published', 'clickup', 'T9', 'T9', ?)""", (cid, month, time.time()))
        with mock.patch.object(wordpress, "home_status", return_value=200), mock.patch.object(wordpress, "posts", return_value=posts), \
                mock.patch.object(wordpress, "check_live", side_effect=lambda u: checks[u]):
            sync.sync_wordpress(c, [0])
        rows = {r["ext_id"]: r for r in self.db.q("SELECT * FROM cc_deliverables WHERE client_id=?", (cid,))}
        self.assertTrue(rows["11"]["verified_at"])
        self.assertIsNone(rows["12"]["verified_at"])
        self.assertEqual(rows["12"]["check_note"], "noindex")
        self.assertEqual(rows["T9"]["merged_into"], rows["11"]["id"])  # the task and the post count once
        self.assertEqual(rows["11"]["task_id"], "T9")

    def test_failed_jobs_retry_then_fail_and_mark_the_source(self):
        from webapp.cc import sync
        cid = self.new_client(name="Flaky")
        with self.env():
            self.db.x("DELETE FROM cc_sync")
            sync.enqueue(cid, "wordpress", "test")
            self.assertFalse(sync.enqueue(cid, "wordpress", "test"))  # no duplicates
            boom = mock.patch.dict(sync.ADAPTERS, {"wordpress": mock.Mock(side_effect=RuntimeError("site timed out"))})
            with boom:
                for _ in range(3):
                    self.db.x("UPDATE cc_sync SET not_before=NULL")
                    row = sync.claim()
                    self.assertIsNotNone(row)
                    sync.run(row)
            j = self.db.q("SELECT * FROM cc_sync WHERE client_id=?", (cid,), one=True)
            self.assertEqual((j["status"], j["attempts"]), ("failed", 3))
            src = self.db.q("SELECT state, detail FROM cc_sources WHERE client_id=? AND source='wordpress'", (cid,), one=True)
            self.assertEqual((src["state"], src["detail"]), ("error", "site timed out"))


class Logos(WithApp):
    PAGE = """<html><head>
      <link rel="icon" href="/fav-32.png" sizes="32x32"><link rel="apple-touch-icon" href="/apple.png">
      <script type="application/ld+json">{"@context":"https://schema.org","@type":"HomeAndConstructionBusiness","logo":{"@type":"ImageObject","url":"https://x.example/ld-logo.png"}}</script>
      </head><body><header><img class="partner-logo" src="/award.png"><a href="/"><img width="200" class="custom-logo" data-src="/wp-content/uploads/logo.svg" src="data:image/gif;base64,R0lGOD"></a></header></body></html>"""

    def test_candidates_in_preference_order(self):
        from webapp.cc.sources import logo
        c = logo.candidates(self.PAGE, "https://x.example/")
        self.assertEqual([u for u, _ in c], ["https://x.example/wp-content/uploads/logo.svg", "https://x.example/ld-logo.png",
                                             "https://x.example/apple.png"])  # partner/award images and small favicons skipped

    def test_sniff_trusts_bytes_not_names(self):
        from webapp.cc.sources import logo
        self.assertEqual(logo.sniff(b"\x89PNG\r\n\x1a\n" + b"0" * 200), "image/png")
        self.assertEqual(logo.sniff(b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>'), "image/svg+xml")
        self.assertIsNone(logo.sniff(b"<html><script>alert(1)</script></html>"))

    def test_logo_route_access_upload_and_safety(self):
        import io
        cid = self.new_client(name="Logo Homes")
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 300
        with self.env():
            c = self.client("ccboss")
            self.assertEqual(c.get(f"/delivery/clients/{cid}/logo").status_code, 404)  # none yet
            r = c.post(f"/delivery/clients/{cid}/logo", data={"csrf": "t", "action": "upload", "file": (io.BytesIO(b"<html>no</html>"), "x.png")},
                       content_type="multipart/form-data")
            self.assertEqual(r.status_code, 302)
            self.assertEqual(c.get(f"/delivery/clients/{cid}/logo").status_code, 404)  # not an image: refused
            c.post(f"/delivery/clients/{cid}/logo", data={"csrf": "t", "action": "upload", "file": (io.BytesIO(png), "logo.png")},
                   content_type="multipart/form-data")
            r = c.get(f"/delivery/clients/{cid}/logo")
            self.assertEqual((r.status_code, r.mimetype), (200, "image/png"))
            self.assertIn("sandbox", r.headers["Content-Security-Policy"])
            self.assertEqual(self.client("ccrep").get(f"/delivery/clients/{cid}/logo").status_code, 404)
            self.assertEqual(self.client().get(f"/delivery/clients/{cid}/logo").status_code, 404)
            # an uploaded logo is never replaced by the automatic lookup
            from webapp.cc import sync
            row = dict(self.db.q("SELECT * FROM cc_clients WHERE id=?", (cid,), one=True))
            with mock.patch("webapp.cc.sources.logo.find", side_effect=AssertionError("should not look")):
                self.assertFalse(sync.maybe_logo(row, [0]))
            page = c.get("/delivery").data
            self.assertIn(f"/delivery/clients/{cid}/logo".encode(), page)
            self.assertIn(b"cc-tiles", page)

    def test_everything_sits_inside_the_panel(self):
        """A stray closing tag once pushed the filters and tiles outside .cc, so its styles (dropdown arrows) never applied."""
        from html.parser import HTMLParser

        class Walk(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack, self.outside = [], []
            def handle_starttag(self, tag, attrs):
                a = dict(attrs)
                if tag in ("select", "form") or "ctile" in (a.get("class") or ""):
                    if not any("cc" in c.split() for c in self.stack):
                        self.outside.append(tag + "." + (a.get("class") or a.get("id") or ""))
                if tag not in ("input", "img", "br", "meta", "link", "hr", "option"):
                    self.stack.append(a.get("class") or "")
            def handle_endtag(self, tag):
                if tag not in ("input", "img", "br", "meta", "link", "hr", "option") and self.stack:
                    self.stack.pop()
        self.new_client(name="Panel Homes")
        with self.env():
            c = self.client("ccboss")
            cid = self.db.q("SELECT id FROM cc_clients WHERE name='Panel Homes'", one=True)["id"]
            for path in ("/delivery", "/delivery/scope", f"/delivery/clients/{cid}"):
                w = Walk()
                w.feed(c.get(path).data.decode())
                self.assertEqual([x for x in w.outside if "rail" not in x], [], path)

    def test_status_line_puts_delivery_before_setup(self):
        from webapp.cc import views
        setup = {"missing": ["Go-live date"], "done": 5, "total": 6}
        self.assertEqual(views.status_line([{"code": "short", "severity": "red"}, {"code": "golive_missing", "severity": "amber"}], setup)["text"],
                         "Delivery: 1 issue")
        self.assertEqual(views.status_line([{"code": "golive_missing", "severity": "amber"}], setup)["text"], "Setup 5/6")
        # change 76: the go-live date is optional: never a flag, never "missing" on the checklist
        from webapp.cc import health
        cx = ctx(client={**ctx()["client"], "go_live": None}, today=D(2026, 11, 19), track_from="2026-11")
        self.assertNotIn("golive_missing", codes(health.evaluate(cx)))
        chk = views.setup_checklist(cx["client"], FULL, "2026-11")
        self.assertNotIn("Go-live date", chk["missing"])
        self.assertEqual(views.status_line([], {"missing": [], "done": 6, "total": 6})["text"], "On track")
        self.assertEqual(views.initials("Conrad & Conrad"), "CC")


class SearchConsole(WithApp):
    """Change 65: Search Console through DataWise's stored copy, and the client page laid out by channel."""
    PROPS = {"connected": True, "needs_reconnect": False, "properties": [
        {"id": "p-url", "site_url": "https://www.sc-homes.example/", "is_enabled": 1, "last_synced_at": "2026-11-19 01:50:27"},
        {"id": "p-dom", "site_url": "sc-domain:sc-homes.example", "is_enabled": 1, "last_synced_at": "2026-11-18 01:00:00"},
        {"id": "p-off", "site_url": "sc-domain:off-homes.example", "is_enabled": 0, "last_synced_at": None},
        {"id": "p-other", "site_url": "sc-domain:other.example", "is_enabled": 1, "last_synced_at": "2026-11-19 01:00:00"}]}
    OVERVIEW = {"overview": {"property": "sc-domain:sc-homes.example", "last_synced": "2026-11-19 01:00:00",
        "range": {"days": 30, "clicks": 9, "impressions": 642, "avg_position": 4.7, "prev_clicks": None,
                  "daily": [{"date": "2026-11-15", "clicks": 0, "impressions": 37}, {"date": "2026-11-16", "clicks": 1, "impressions": 34}]},
        "summary": {"last_7_days": {"total_clicks": 2, "total_impressions": 166, "avg_position": 4.5},
                    "last_30_days": {"total_clicks": 9, "total_impressions": 642, "avg_position": 4.7},
                    "last_90_days": {"total_clicks": 9, "total_impressions": 642, "avg_position": 4.7}},
        "query_summary": {"total_queries": 23, "top_3": 13, "top_10": 22, "striking_distance": 0},
        "top_queries": [{"query": "sc homes charlotte", "clicks": 2, "impressions": 24, "avg_position": 3.6, "avg_ctr": 0.19}],
        "top_pages": [{"page": "https://www.sc-homes.example/?utm_source=google&utm_medium=organic", "clicks": 6, "impressions": 174, "avg_position": 5.7}]}}

    class Fake:
        def __init__(self, props, overview):
            self.props, self.overview, self.seen = props, overview, []
        def call(self, tool, args):
            self.seen.append((tool, args))
            return self.props if args["action"] == "list_properties" else self.overview

    def test_picks_the_switched_on_domain_property(self):
        from webapp.cc.sources import gsc
        best, cands = gsc.match(self.PROPS["properties"], "https://www.sc-homes.example/about")
        self.assertEqual(best["id"], "p-dom")  # Domain property beats URL-prefix, even if synced a bit earlier
        self.assertEqual(len(cands), 2)
        self.assertEqual(gsc.match(self.PROPS["properties"], "off-homes.example")[0], None)
        self.assertEqual(gsc.page_path("https://x.example/a?utm_source=g&id=3"), ("/a?id=3", True))
        s = gsc.summarize(self.OVERVIEW, "sc-domain:sc-homes.example")
        self.assertIsNone(s["prev"]["clicks"])  # no previous period stays unknown, never 0
        self.assertAlmostEqual(s["d30"]["ctr"], 9 / 642)
        self.assertEqual(s["data_through"], "2026-11-16")

    def test_sync_reads_datawise_and_the_page_shows_every_channel(self):
        from webapp.cc import sync
        cid = self.new_client(name="SC Homes", domain="sc-homes.example")
        fake = self.Fake(self.PROPS, self.OVERVIEW)
        row = dict(self.db.q("SELECT * FROM cc_clients WHERE id=?", (cid,), one=True))
        with mock.patch.object(sync, "_datawise", return_value=fake), mock.patch.object(sync, "today", return_value=D(2026, 11, 19)):
            sync.sync_gsc(row, [0])
        self.assertTrue(all(a.get("action") in ("list_properties", "overview") for _, a in fake.seen))  # read-only calls only
        self.assertEqual(fake.seen[1][1]["property_id"], "p-dom")
        src = self.db.q("SELECT state FROM cc_sources WHERE client_id=? AND source='gsc'", (cid,), one=True)
        self.assertEqual(src["state"], "ok")
        with self.env():
            page = self.client("ccboss").get(f"/delivery/clients/{cid}").data.decode()
        for part in ("Content &amp; SEO", "Search Console", "Business Profile", "Analytics", "Clicks per day", "sc homes charlotte", "642"):
            self.assertIn(part, page)
        self.assertIn("no previous period to compare", page)

    def test_switched_off_or_missing_property_is_flagged_not_zero(self):
        from webapp.cc import data, sync
        cid = self.new_client(name="Off Homes", domain="off-homes.example")
        row = dict(self.db.q("SELECT * FROM cc_clients WHERE id=?", (cid,), one=True))
        with mock.patch.object(sync, "_datawise", return_value=self.Fake(self.PROPS, self.OVERVIEW)):
            sync.sync_gsc(row, [0])
        data.recompute(cid)
        flag = self.db.q("SELECT reason FROM cc_flags WHERE client_id=? AND fkey='source:gsc' AND resolved_at IS NULL", (cid,), one=True)
        self.assertIn("switched off", flag["reason"])
        self.assertIsNone(data.metric(cid, "gsc", "overview"))
        with self.env():
            page = self.client("ccboss").get(f"/delivery/clients/{cid}").data.decode()
        self.assertIn("switch syncing on for this property", page)

    def test_client_page_survives_sitemap_baseline_rows(self):
        cid = self.new_client(name="Baseline Homes", domain="baseline.example")
        self.db.x("""INSERT INTO cc_deliverables (client_id, item, month, title, url, status, source, ext_id, updated_at)
                     VALUES (?, 'blog', 'baseline', 'old-post', 'https://baseline.example/old-post/', 'published', 'sitemap', 'b1', ?)""",
                  (cid, time.time()))
        with self.env():
            r = self.client("ccboss").get(f"/delivery/clients/{cid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Earlier post", r.data)  # shown as a blog card (change 74) ...
        from webapp.cc import data as _d
        self.assertFalse([x for x in _d.context(cid)["deliverables"] if x["month"] == "baseline"])  # ... but never counted as delivered


class BusinessProfile(WithApp):
    """Change 66: Google Business Profile, read-only, with an encrypted stored sign-in."""
    KEYS = {"GBP_OAUTH_CLIENT_ID": "test-client.apps.googleusercontent.com", "GBP_OAUTH_CLIENT_SECRET": "GOCSPX-test"}

    def setUp(self):
        from aeo import kv
        from webapp.cc.sources import gbp
        gbp.forget()
        kv.update("cc_gbp_dir_at", lambda _o: 0, 0)
        self.db.x("DELETE FROM cc_gbp_locations")
        self.keys = mock.patch.object(gbp.config, "key", side_effect=lambda n: self.KEYS.get(n))
        self.keys.start()
        self.addCleanup(self.keys.stop)

    @staticmethod
    def iso(days_ago):
        return datetime.datetime.utcfromtimestamp(time.time() - days_ago * 86400).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")

    def fake_google(self, sent, quota_zero=False):
        import io
        import urllib.error

        class Resp:
            def __init__(self, body):
                self.body = json.dumps(body).encode()
            def read(self, *_):
                return self.body
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False

        iso = self.iso

        def handler(req, timeout):
            url, method = req.full_url, req.get_method()
            sent.append((method, url.split("?")[0], dict(req.header_items())))
            if "oauth2.googleapis.com/token" in url:
                return Resp({"access_token": "ya29.test-access", "expires_in": 3600})
            if quota_zero:
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, io.BytesIO(json.dumps(
                    {"error": {"code": 429, "message": "Quota exceeded for quota metric 'Requests' and limit 'Requests per minute'", "status": "RESOURCE_EXHAUSTED"}}).encode()))
            if url.startswith(gbp_mod.ACCOUNTS):
                return Resp({"accounts": [{"name": "accounts/1", "accountName": "HBM agency"}]})
            if "/accounts/1/locations?" in url and "mybusinessbusinessinformation" in url:
                return Resp({"locations": [
                    {"name": "locations/11", "title": "GBP Homes", "websiteUri": "https://www.gbp-homes.example/?utm_source=gbp",
                     "metadata": {"placeId": "ChIJ1", "mapsUri": "https://maps.google.com/?cid=1", "hasVoiceOfMerchant": True},
                     "openInfo": {"status": "OPEN"}, "storefrontAddress": {"addressLines": ["1 Main St"], "locality": "Austin", "administrativeArea": "TX"}},
                    {"name": "locations/22", "title": "Someone Else", "websiteUri": "https://else.example/", "metadata": {"hasVoiceOfMerchant": True}}]})
            if ":fetchMultiDailyMetricsTimeSeries" in url:
                return Resp({"multiDailyMetricTimeSeries": [{"dailyMetricTimeSeries": [
                    {"dailyMetric": "CALL_CLICKS", "timeSeries": {"datedValues": [{"date": {"year": 2026, "month": 11, "day": 10}, "value": "3"},
                                                                                    {"date": {"year": 2026, "month": 11, "day": 11}}]}},
                    {"dailyMetric": "BUSINESS_IMPRESSIONS_MOBILE_MAPS", "timeSeries": {"datedValues": [{"date": {"year": 2026, "month": 11, "day": 10}, "value": "40"}]}},
                    {"dailyMetric": "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH", "timeSeries": {"datedValues": [{"date": {"year": 2026, "month": 11, "day": 10}, "value": "2"}]}}]}]})
            if url.split("?")[0].endswith("/reviews"):
                return Resp({"averageRating": 4.8, "totalReviewCount": 3, "reviews": [
                    {"reviewId": "r1", "starRating": "FIVE", "comment": "Great build", "createTime": iso(10), "reviewer": {"displayName": "Jane Smith"},
                     "reviewReply": {"comment": "Thank you!", "updateTime": iso(9)}},
                    {"reviewId": "r2", "starRating": "TWO", "comment": "Slow", "createTime": iso(6), "reviewer": {"displayName": "Bob"}},
                    {"reviewId": "r3", "starRating": "FIVE", "createTime": iso(1)}]})
            if url.split("?")[0].endswith("/localPosts"):
                return Resp({"localPosts": [{"name": "accounts/1/locations/11/localPosts/p1", "summary": "New kitchen finished", "createTime": iso(2),
                                             "state": "LIVE", "searchUrl": "https://local.google.com/p1"},
                                            {"name": "accounts/1/locations/11/localPosts/p2", "summary": "Rejected one", "createTime": iso(3), "state": "REJECTED"}]})
            if url.split("?")[0].endswith("/media"):
                return Resp({"mediaItems": [{"name": "accounts/1/locations/11/media/m1", "mediaFormat": "PHOTO", "createTime": iso(4),
                                             "locationAssociation": {"category": "EXTERIOR"}, "googleUrl": "https://lh3.example/m1"},
                                            {"name": "accounts/1/locations/11/media/v1", "mediaFormat": "VIDEO", "createTime": iso(4)}]})
            raise AssertionError("unexpected request " + url)

        from webapp.cc.sources import gbp as gbp_mod
        return handler

    def test_sign_in_is_stored_encrypted(self):
        from aeo import kv
        from webapp.cc.sources import gbp
        gbp.save("1//refresh-SECRET-value", "lav@example.com", "ccboss@homebuildermarketers.com")
        self.assertNotIn("refresh-SECRET-value", json.dumps(kv.get(gbp.KV_KEY)))
        self.assertEqual(gbp._refresh_token(), "1//refresh-SECRET-value")
        self.assertEqual(gbp.stored()["email"], "lav@example.com")
        self.assertNotIn("enc", gbp.stored())
        self.assertTrue(gbp.connected())

    def test_sync_reads_only_and_counts_what_google_shows(self):
        from webapp.cc import data, gbp_sync, health, ledger
        from webapp.cc.core import month_of, today
        from webapp.cc.sources import gbp
        gbp.save("1//refresh", "lav@example.com", "ccboss@homebuildermarketers.com")
        cid = self.new_client(name="GBP Homes", domain="gbp-homes.example")
        for item, q in (("gbp_post", 4), ("gbp_photo", 1), ("review_reply", 1)):
            self.db.x("INSERT INTO cc_scope (client_id, item, qty, start_month, set_by, set_at) VALUES (?,?,?,?,?,?)",
                      (cid, item, q, "2026-01", "test", time.time()))
        sent = []
        with mock.patch.object(gbp, "_open", side_effect=self.fake_google(sent)):
            note = gbp_sync.sync(dict(self.db.q("SELECT * FROM cc_clients WHERE id=?", (cid,), one=True)), [0])
        self.assertIn("GBP Homes", note)
        api = [(m, u) for m, u, _ in sent if "oauth2.googleapis.com" not in u]
        self.assertTrue(api and all(m == "GET" for m, _ in api), api)  # read-only towards the profiles
        self.assertTrue(all(h.get("Authorization", "").startswith("Bearer ") for m, u, h in sent if "oauth2" not in u))
        row = self.db.q("SELECT gbp_location FROM cc_clients WHERE id=?", (cid,), one=True)
        self.assertEqual(row["gbp_location"], "locations/11")  # matched by website, utm and www ignored
        dl = {d["ext_id"]: d for d in self.db.q("SELECT * FROM cc_deliverables WHERE client_id=? AND source='gbp'", (cid,))}
        self.assertIn("accounts/1/locations/11/localPosts/p1", dl, (note, list(dl)))
        self.assertTrue(dl["accounts/1/locations/11/localPosts/p1"]["verified_at"])
        self.assertIsNone(dl["accounts/1/locations/11/localPosts/p2"]["verified_at"])  # rejected by Google: listed, not counted
        self.assertIn("accounts/1/locations/11/media/m1", dl)
        self.assertIn("accounts/1/locations/11/media/v1", dl)  # videos count toward "photos & videos"
        self.assertTrue(dl["reply:r1"]["verified_at"])
        daily = json.loads(data.metric(cid, "gbp", "daily")["text"])
        self.assertEqual(daily[0], {"date": "2026-11-10", "calls": 3, "website": 0, "directions": 0, "conversations": 0, "views": 42})
        data.recompute(cid)
        flags = {f["code"]: f for f in self.db.q("SELECT * FROM cc_flags WHERE client_id=? AND resolved_at IS NULL", (cid,))}
        self.assertIn("gbp_reviews", flags)  # the 2-star review has waited 6 days
        self.assertNotIn("gbp_no_post", flags)  # a live post 2 days ago
        self.assertNotIn("source_off", {c for c in flags if flags[c]["fkey"] == "source:gbp"})
        ctx = data.context(cid)
        cell = ledger.cell(None, ctx["scope"], ctx["deliverables"], "gbp_photo", month_of(today()), today(), True)
        self.assertEqual(cell["counted"], 2 if month_of(datetime.date.fromtimestamp(time.time() - 4 * 86400)) == month_of(today()) else 0)  # photo + video
        stored = json.loads(data.metric(cid, "gbp", "reviews")["text"])
        self.assertEqual(stored["waiting"], 2)
        self.assertNotIn("Smith", json.dumps(stored))  # reviewer names aren't kept
        with self.env():
            page = self.client("ccboss").get(f"/delivery/clients/{cid}").data.decode()
        for part in ("Call clicks per day", "Open on Google Maps", "waiting for a reply", "GBP Homes"):
            self.assertIn(part, page)

    def test_quota_zero_is_not_approved_not_zero(self):
        from webapp.cc import data, gbp_sync
        from webapp.cc.sources import gbp
        gbp.save("1//refresh", "lav@example.com", "ccboss@homebuildermarketers.com")
        cid = self.new_client(name="Quota Homes", domain="quota.example")
        with mock.patch.object(gbp, "_open", side_effect=self.fake_google([], quota_zero=True)), mock.patch.object(gbp.time, "sleep"):
            gbp_sync.sync(dict(self.db.q("SELECT * FROM cc_clients WHERE id=?", (cid,), one=True)), [0])
        src = self.db.q("SELECT state, detail FROM cc_sources WHERE client_id=? AND source='gbp'", (cid,), one=True)
        self.assertEqual(src["state"], "not_connected")
        self.assertIn("quota", src["detail"])
        self.assertIsNone(data.metric(cid, "gbp", "daily"))

    def test_sign_in_routes_are_guarded(self):
        from webapp.cc.sources import gbp
        with self.env():
            self.assertEqual(self.client("ccadmin2").get("/delivery/gbp/connect").status_code, 404)
            c = self.client("ccboss")
            self.assertEqual(c.get("/delivery/gbp/connect", headers={"Sec-Fetch-Site": "cross-site"}).status_code, 403)
            r = c.get("/delivery/gbp/connect", headers={"Sec-Fetch-Site": "same-origin"})
            self.assertEqual(r.status_code, 302)
            self.assertTrue(r.headers["Location"].startswith(gbp.AUTH_URL))
            self.assertIn("code_challenge_method=S256", r.headers["Location"])
            self.assertIn("access_type=offline", r.headers["Location"])
            r = c.get("/delivery/gbp/callback?state=forged&code=x")  # wrong state: refused, nothing stored
            self.assertEqual(r.status_code, 302)
            self.assertIsNone(gbp.stored())
            page = c.get("/delivery/sync").data.decode()
            self.assertIn("Connect Business Profile", page)

    def test_profile_choice_by_hand_or_none(self):
        from webapp.cc import gbp_sync
        cid = self.new_client(name="Choice Homes", domain="choice.example")
        self.db.x("INSERT INTO cc_gbp_locations (name, account, title, website, seen_at) VALUES ('locations/9', 'accounts/1', 'Choice HQ', NULL, ?)", (time.time(),))
        with self.env():
            c = self.client("ccboss")
            self.assertEqual(c.post(f"/delivery/clients/{cid}/gbp", data={"csrf": "t", "gbp": "locations/404"}).status_code, 400)
            c.post(f"/delivery/clients/{cid}/gbp", data={"csrf": "t", "gbp": "locations/9"})
        row = dict(self.db.q("SELECT * FROM cc_clients WHERE id=?", (cid,), one=True))
        self.assertEqual((row["gbp_how"], row["gbp_location"]), ("manual", "locations/9"))
        self.assertEqual(gbp_sync.match(row, gbp_sync.directory())[0]["title"], "Choice HQ")
        self.assertEqual(gbp_sync.match({**row, "gbp_how": "auto", "gbp_location": None, "name": "Choice  Homes LLC"},
                                        [{"name": "l1", "title": "Choice Homes", "website": None}])[0]["name"], "l1")  # name match
        with self.env():
            self.client("ccboss").post(f"/delivery/clients/{cid}/gbp", data={"csrf": "t", "gbp": "none"})
        row = dict(self.db.q("SELECT * FROM cc_clients WHERE id=?", (cid,), one=True))
        self.assertEqual(gbp_sync.match(row, gbp_sync.directory())[1], "none")


class Analytics(WithApp):
    """Change 67: Google Analytics 4, read-only (analytics.readonly), matched to clients by web-stream website."""
    KEYS = BusinessProfile.KEYS

    def setUp(self):
        from aeo import kv
        from webapp.cc.sources import ga4
        ga4.forget()
        kv.update("cc_ga4_dir_at", lambda _o: 0, 0)
        self.db.x("DELETE FROM cc_ga4_properties")
        self.keys = mock.patch.object(ga4._g.config, "key", side_effect=lambda n: self.KEYS.get(n))
        self.keys.start()
        self.addCleanup(self.keys.stop)

    def fake_ga(self, sent, quiet=False):
        from webapp.cc.sources import ga4

        class Resp:
            def __init__(self, body):
                self.body = json.dumps(body).encode()
            def read(self, *_):
                return self.body
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False

        def rows(dims, mets, data_):
            return {"dimensionHeaders": [{"name": d} for d in dims], "metricHeaders": [{"name": m} for m in mets],
                    "rows": [{"dimensionValues": [{"value": v} for v in r[0]], "metricValues": [{"value": str(v)} for v in r[1]]} for r in data_]}

        def handler(req, timeout):
            url, method = req.full_url, req.get_method()
            body = json.loads(req.data.decode()) if req.data and "runReport" in url else None
            sent.append((method, url.split("?")[0], body))
            if "oauth2.googleapis.com/token" in url:
                return Resp({"access_token": "ya29.ga", "expires_in": 3600})
            if url.startswith(ga4.ADMIN + "/accountSummaries"):
                return Resp({"accountSummaries": [{"account": "accounts/5", "displayName": "HBM clients", "propertySummaries": [
                    {"property": "properties/77", "displayName": "GA Homes - GA4"}, {"property": "properties/88", "displayName": "Other"}]}]})
            if "/dataStreams" in url:
                site = "https://www.ga-homes.example/" if "/77/" in url else "https://other.example"
                return Resp({"dataStreams": [{"type": "WEB_DATA_STREAM", "webStreamData": {"defaultUri": site, "measurementId": "G-TEST"}}]})
            if url.endswith(":runReport"):
                dims = [d["name"] for d in body.get("dimensions") or []]
                start = body["dateRanges"][0]["startDate"]
                if dims == ["sessionDefaultChannelGroup"]:
                    org = 100 if start >= "2026-10" else (80 if start >= "2026-09" else 50)
                    return Resp(rows(dims, ["sessions", "engagedSessions", "keyEvents", "totalUsers"],
                                     [] if quiet else [(["Organic Search"], [org, org // 2, 4, org]), (["Direct"], [40, 20, 0 if quiet else 1, 30])]))
                if dims == ["date", "sessionDefaultChannelGroup"]:
                    return Resp(rows(dims, ["sessions"], [] if quiet else [(["20261117", "Organic Search"], [5]), (["20261117", "Direct"], [2])]))
                if dims == ["landingPagePlusQueryString"]:
                    return Resp(rows(dims, ["sessions", "engagedSessions", "keyEvents"], [(["/pool-resurfacing/"], [30, 20, 2])]))
                if dims == ["sessionSource"]:
                    return Resp(rows(dims, ["sessions", "keyEvents"], [(["chatgpt.com"], [3, 1])]))
                if dims == ["eventName"]:
                    return Resp(rows(dims, ["keyEvents"], [(["generate_lead"], [5])]))
            raise AssertionError("unexpected " + method + " " + url)
        return handler

    def test_reads_only_and_builds_the_analytics_tab(self):
        from webapp.cc import data, ga4_sync
        from webapp.cc.sources import ga4
        ga4.save("1//ga-refresh", "seo@example.com", "ccboss@homebuildermarketers.com")
        cid = self.new_client(name="GA Homes", domain="ga-homes.example")
        sent = []
        with mock.patch.object(ga4, "_open", side_effect=self.fake_ga(sent)), mock.patch.object(ga4_sync, "today", return_value=D(2026, 11, 19)):
            note = ga4_sync.sync(dict(self.db.q("SELECT * FROM cc_clients WHERE id=?", (cid,), one=True)), [0])
        self.assertIn("100 organic sessions", note)
        for method, url, _ in sent:
            if "oauth2" in url:
                continue
            self.assertTrue((method == "GET" and url.startswith(ga4.ADMIN)) or (method == "POST" and url.endswith(":runReport")), (method, url))
        self.assertEqual(self.db.q("SELECT ga4_property FROM cc_clients WHERE id=?", (cid,), one=True)["ga4_property"], "properties/77")
        sm = json.loads(data.metric(cid, "ga4", "summary")["text"])
        self.assertEqual((sm["cur"]["sessions"], sm["prev"]["sessions"], sm["ly"]["sessions"]), (100, 80, 50))
        self.assertEqual(sm["periods"]["cur"], ["2026-10-20", "2026-11-18"])  # 30 days ending yesterday
        self.assertEqual(sm["periods"]["ly"], ["2025-10-20", "2025-11-18"])
        self.assertEqual(len(sm["daily"]), 90)
        self.assertEqual(sm["ai_sessions"], 3)
        ai_body = next(b for m, u, b in sent if b and b.get("dimensions") == [{"name": "sessionSource"}])
        self.assertIn("chatgpt\\.com", ai_body["dimensionFilter"]["filter"]["stringFilter"]["value"])
        with self.env():
            page = self.client("ccboss").get(f"/delivery/clients/{cid}").data.decode()
        for part in ("Organic sessions per day", "pool-resurfacing", "generate_lead", "AI-assistant referrals", "vs same 30 days last year"):
            self.assertIn(part, page)

    def test_refuses_anything_but_reads(self):
        from webapp.cc.sources import ga4
        ga4.save("1//ga-refresh", "seo@example.com", "x")
        with self.assertRaises(ga4.GA4Error):
            ga4._call("POST", ga4.ADMIN + "/properties/77/dataStreams", {"x": 1})
        with self.assertRaises(ga4.GA4Error):
            ga4._call("DELETE", ga4.ADMIN + "/properties/77", None)

    def test_quiet_tag_is_flagged(self):
        from webapp.cc import data, ga4_sync
        from webapp.cc.sources import ga4
        ga4.save("1//ga-refresh", "seo@example.com", "x")
        cid = self.new_client(name="Quiet Homes", domain="ga-homes.example")
        with mock.patch.object(ga4, "_open", side_effect=self.fake_ga([], quiet=True)):
            ga4_sync.sync(dict(self.db.q("SELECT * FROM cc_clients WHERE id=?", (cid,), one=True)), [0])
        data.recompute(cid)
        codes_ = {r["code"] for r in self.db.q("SELECT code FROM cc_flags WHERE client_id=? AND resolved_at IS NULL", (cid,))}
        self.assertIn("ga4_no_data", codes_)

    def test_sign_in_routes_and_choice(self):
        from webapp.cc.sources import ga4
        cid = self.new_client(name="Choice GA", domain="choice-ga.example")
        self.db.x("INSERT INTO cc_ga4_properties (name, display_name, seen_at) VALUES ('properties/9', 'Choice GA4', ?)", (time.time(),))
        with self.env():
            self.assertEqual(self.client("ccadmin2").get("/delivery/ga4/connect").status_code, 404)
            c = self.client("ccboss")
            r = c.get("/delivery/ga4/connect", headers={"Sec-Fetch-Site": "same-origin"})
            self.assertIn("analytics.readonly", urllib_unquote(r.headers["Location"]))
            self.assertNotIn("business.manage", urllib_unquote(r.headers["Location"]))
            c.get("/delivery/ga4/callback?state=forged&code=x")
            self.assertIsNone(ga4.stored())
            self.assertEqual(c.post(f"/delivery/clients/{cid}/ga4", data={"csrf": "t", "ga4": "properties/404"}).status_code, 400)
            c.post(f"/delivery/clients/{cid}/ga4", data={"csrf": "t", "ga4": "properties/9"})
            self.assertIn("Connect Analytics", c.get("/delivery/sync").data.decode())
        row = self.db.q("SELECT ga4_how, ga4_property FROM cc_clients WHERE id=?", (cid,), one=True)
        self.assertEqual((row["ga4_how"], row["ga4_property"]), ("manual", "properties/9"))


def urllib_unquote(s):
    import urllib.parse
    return urllib.parse.unquote(s)


class Bing(WithApp):
    """Change 68: Bing Webmaster Tools (one API key, read methods only)."""

    class Resp:
        def __init__(self, body):
            self.body = json.dumps(body).encode()
        def read(self, *_):
            return self.body
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False

    @staticmethod
    def msdate(days_ago):
        return "/Date(%d-0700)/" % int((time.time() - days_ago * 86400) * 1000)

    def setUp(self):
        from aeo import kv
        kv.update("cc_bing_sites", lambda _o: {}, {})

    def test_bing_reads_only_and_never_leaks_the_key(self):
        from webapp.cc import bing_sync, data
        from webapp.cc.sources import bing
        cid = self.new_client(name="Bing Homes", domain="bing-homes.example")
        sent, md = [], self.msdate

        def handler(req, timeout):
            url = req.full_url
            sent.append((req.get_method(), url))
            method = url.split("/json/")[1].split("?")[0]
            if method == "GetUserSites":
                return self.Resp({"d": [{"Url": "http://bing-homes.example/", "IsVerified": False},
                                        {"Url": "https://www.bing-homes.example/", "IsVerified": True}]})
            if method == "GetRankAndTrafficStats":
                return self.Resp({"d": [{"Date": md(d), "Clicks": 2, "Impressions": 50} for d in range(1, 70)]})
            if method == "GetQueryStats":
                return self.Resp({"d": [{"Date": md(3), "Query": "deck builder", "Clicks": 4, "Impressions": 100, "AvgImpressionPosition": 3.0},
                                        {"Date": md(10), "Query": "deck builder", "Clicks": 1, "Impressions": 300, "AvgImpressionPosition": 7.0},
                                        {"Date": md(80), "Query": "old query", "Clicks": 9, "Impressions": 9}]})
            if method == "GetPageStats":
                return self.Resp({"d": [{"Date": md(3), "Query": "https://www.bing-homes.example/decks/", "Clicks": 5, "Impressions": 120}]})
            if method == "GetCrawlStats":
                return self.Resp({"d": [{"Date": md(2), "InIndex": 140, "CrawledPages": 30, "CrawlErrors": 2, "Code4xx": 2, "Code5xx": 0}]})
            raise AssertionError(url)

        with mock.patch.object(bing.config, "key", side_effect=lambda n: "BINGKEY123" if n == "BING_WEBMASTER_API_KEY" else None), \
                mock.patch.object(bing, "_open", side_effect=handler):
            note = bing_sync.sync(dict(self.db.q("SELECT * FROM cc_clients WHERE id=?", (cid,), one=True)), [0])
            with self.assertRaises(bing.BingError):
                bing.get("SubmitUrl", {"siteUrl": "x", "url": "y"})
        self.assertTrue(all(m == "GET" for m, _ in sent))
        self.assertTrue({u.split("/json/")[1].split("?")[0] for _, u in sent} <= bing.READS)
        sm = json.loads(data.metric(cid, "bing", "summary")["text"])
        self.assertEqual(sm["site"], "https://www.bing-homes.example/")  # the verified https one
        q = sm["queries"][0]
        self.assertEqual((q["key"], q["clicks"], q["impressions"], q["position"]), ("deck builder", 5, 400, 6.0))  # weighted
        self.assertNotIn("old query", [x["key"] for x in sm["queries"]])
        self.assertEqual(sm["d30"]["clicks"], 2 * 30)  # yesterday back to 30 days ago
        self.assertEqual(sm["crawl"]["in_index"], 140)
        self.assertIn("clicks / 30 days", note)
        with self.env():
            page = self.client("ccboss").get(f"/delivery/clients/{cid}").data.decode()
        self.assertIn("Top Bing queries", page)
        self.assertNotIn("BINGKEY123", page)

    def test_bing_error_hides_the_key(self):
        import io
        import urllib.error
        from webapp.cc.sources import bing

        def handler(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 400, "Bad", {}, io.BytesIO(b'{"Message": "Bad request for apikey BINGKEY123"}'))
        with mock.patch.object(bing.config, "key", side_effect=lambda n: "BINGKEY123"), mock.patch.object(bing, "_open", side_effect=handler):
            with self.assertRaises(bing.BingError) as e:
                bing.get("GetUserSites")
        self.assertNotIn("BINGKEY123", str(e.exception))


class TrackingStart(unittest.TestCase):
    """Change 71: delivery is judged from the tracking start month (November 2026); earlier months aren't owed or flagged."""
    def ts(self, d):
        return datetime.datetime(d.year, d.month, d.day, 12, tzinfo=core.TZ).timestamp()

    def test_months_before_the_start_are_not_judged(self):
        c = ledger.cell("2026-02-01", FULL, [], "blog", "2026-10", D(2026, 11, 19), start="2026-11")
        self.assertEqual(c["state"], "not_tracked")
        self.assertEqual(ledger.cell("2026-02-01", FULL, [], "blog", "2026-11", D(2026, 11, 19), start="2026-11")["state"], "open")
        self.assertEqual(ledger.first_owed_month("2026-09-06", "2026-11"), "2026-11")
        self.assertEqual(ledger.first_owed_month("2027-01-10", "2026-11"), "2027-02")  # a later launch still wins
        self.assertEqual(ledger.months_shown("2026-11", "2026-11")[:2], ["2026-11", "2026-12"])
        self.assertEqual(len(ledger.months_shown("2026-11", "2026-11")), 6)  # the coming months show too
        self.assertEqual(ledger.months_shown("2026-11", "2028-03")[0], "2027-04")  # never more than 12 back

    def test_no_short_or_overdue_flags_from_before_the_start(self):
        old_task = task("blog", due=self.ts(D(2026, 10, 20)), tid="old")  # due in October, still open
        new_task = task("blog", due=self.ts(D(2026, 11, 10)), tid="new")  # due this month, overdue
        cx = ctx(today=D(2026, 11, 19), tasks=[old_task, new_task], track_from="2026-11")
        f = health.evaluate(cx)
        self.assertNotIn("short", codes(f))  # October isn't judged
        overdue = next(x for x in f if x["code"] == "overdue_tasks")
        self.assertEqual(overdue["evidence"]["tasks"], ["new"])
        # without a start, the same client shows October as short
        self.assertIn("short", codes(health.evaluate(ctx(today=D(2026, 11, 19), tasks=[old_task, new_task]))))


class ReviewRule(unittest.TestCase):
    """Change 72: review replies follow a rule (every new review, within 1 or 2 business days by package)."""
    def test_every_review_is_neither_missing_nor_a_count(self):
        rows = FULL[:3] + [scope("review_reply", core.EVERY, "2026-01"), scope("report", 1, "2026-01")]
        self.assertEqual(ledger.cell(None, rows, [], "review_reply", "2026-11", D(2026, 11, 19))["state"], "rule")
        self.assertNotIn("scope_missing", codes(health.evaluate(ctx(scope=rows, today=D(2026, 11, 19)))))

    def test_reply_deadline_depends_on_the_package(self):
        prof = {"text": json.dumps({"title": "X", "verified": 1, "last_post": time.time()})}
        rv = {"text": json.dumps({"waiting": 1, "oldest_waiting": datetime.datetime(2026, 11, 17, 12, tzinfo=core.TZ).timestamp()})}
        metrics = {("gbp", "profile", "latest"): prof, ("gbp", "reviews", "latest"): rv}
        rows = FULL[:3] + [scope("review_reply", core.EVERY, "2026-01"), scope("report", 1, "2026-01")]
        now_ = datetime.datetime(2026, 11, 19, 12, tzinfo=core.TZ).timestamp()  # Thursday: 2 business days after Tuesday
        growth = health.gbp_flags({"package": "Growth", "domain": "x.example"}, rows, metrics, D(2026, 11, 19), now_)
        scale = health.gbp_flags({"package": "Scale", "domain": "x.example"}, rows, metrics, D(2026, 11, 19), now_)
        self.assertNotIn("gbp_reviews", codes(growth))  # 2 business days is within the Growth rule
        self.assertIn("gbp_reviews", codes(scale))  # Scale replies within 1
        none_ = FULL[:3] + [scope("review_reply", 0, "2026-01"), scope("report", 1, "2026-01")]
        self.assertNotIn("gbp_reviews", codes(health.gbp_flags({"package": "Scale"}, none_, metrics, D(2026, 11, 19), now_)))


class LogoQuality(unittest.TestCase):
    """Change 73: the logo finder measures candidates and keeps the sharpest full logo."""
    def test_dims_and_score_prefer_big_wordmarks_over_tiny_files(self):
        import struct
        from webapp.cc.sources import logo
        def png(w, h):
            return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", w, h) + b"\x00" * 120
        self.assertEqual(logo.dims(png(640, 200), "image/png"), (640, 200))
        tiny, big, icon = png(74, 40), png(900, 260), png(512, 512)
        self.assertLess(logo.score(tiny, "image/png", "header logo image"), logo.score(big, "image/png", "logo in structured data"))
        self.assertLess(logo.score(icon, "image/png", "site app icon"), logo.score(big, "image/png", "logo in structured data"))
        self.assertEqual(logo.score(png(40, 20), "image/png", "header logo image"), -1)
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 90"><path d="M0 0h10v10z"/></svg>'
        self.assertEqual(logo.dims(svg, "image/svg+xml"), (300.0, 90.0))
        self.assertGreater(logo.score(svg, "image/svg+xml", "logo in structured data"), logo.score(png(600, 200), "image/png", "logo in structured data"))
        self.assertGreater(logo.score(icon, "image/png", "logo in structured data"), logo.score(svg, "image/svg+xml", "header logo image"))  # declared beats a header svg
        toks = logo.brand_tokens("marcello.homes", "Marcello Homes")
        ico = png(509, 423)
        self.assertGreater(logo.score(svg, "image/svg+xml", "header logo image", "https://marcello.homes/u/marcello-logo-dark.svg", toks),
                           logo.score(ico, "image/png", "logo in structured data", "https://marcello.homes/u/cropped-marcello-fev-icon.png", toks))
        hbm = logo.brand_tokens("homebuildermarketers.com", "Home Builder Marketers")
        self.assertLess(logo.score(svg, "image/svg+xml", "header logo image", "https://homebuildermarketers.com/u/apb-logo.svg", hbm),
                        logo.score(icon, "image/png", "logo in structured data", "https://homebuildermarketers.com/u/Fev-icon_HBM.png", hbm))
        self.assertEqual(logo.full_size("https://static.wixstatic.com/media/ab_1~mv2.png/v1/fill/w_74,h_40/2.png"), "https://static.wixstatic.com/media/ab_1~mv2.png")
        page = '<header><a class="site-logo"><svg viewBox="0 0 10 10">' + '<path d="M0 0h1v1z"/>' * 30 + '</svg></a></header>'
        self.assertTrue(logo.inline_svg(page).startswith(b'<svg xmlns='))
        self.assertGreater(logo.score(big, "image/png", "header logo image"), logo.score(logo.inline_svg(page), "image/svg+xml", "inline header logo"))
        # a review widget's Google logo or a lender's badge on the client's site is never taken as their logo
        html = ('<img class="ti-logo" src="https://cdn.trustindex.io/assets/platform/Google/logo.svg">'
                '<img src="https://acme.example/wp-content/uploads/uwm-logo.svg" alt="UWM logo">'
                '<img class="site-logo" src="https://acme.example/wp-content/uploads/acme-logo.png">')
        urls = [u for u, _ in logo.candidates(html, "https://acme.example/")]
        self.assertEqual(urls, ["https://acme.example/wp-content/uploads/acme-logo.png"])


class BlogCards(WithApp):
    """Change 74: a card per blog post, with the post's own image fetched safely and a link to the live post."""
    def test_share_image_and_title_come_from_the_page(self):
        from webapp.cc.sources import wordpress
        html_ = ('<html><head><title>Deck ideas | Acme Builders</title><meta property="og:title" content="Five deck ideas &amp; costs">'
                 '<meta property="og:image" content="/wp-content/uploads/deck.jpg"></head></html>')
        self.assertEqual(wordpress.page_image(html_, "https://acme.example/blog/deck/"), "https://acme.example/wp-content/uploads/deck.jpg")
        self.assertEqual(wordpress.page_title(html_), "Five deck ideas & costs")

    def test_cards_render_and_image_route_is_guarded(self):
        cid = self.new_client(name="Card Homes", domain="card-homes.example")
        did = self.db.x("""INSERT INTO cc_deliverables (client_id, item, month, title, status, url, published_at, source, ext_id, updated_at, image_url, verified_at)
                           VALUES (?, 'blog', '2026-11', 'Pool deck costs in 2026', 'published', 'https://card-homes.example/blog/pool-deck/', ?, 'wp', 'p1', ?, 'https://card-homes.example/img.jpg', ?)""",
                        (cid, time.time(), time.time(), time.time()))
        jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 400

        class Resp:
            status = 200
            def read(self, *_):
                return jpeg
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
        from aeo import netsafe
        with self.env():
            c = self.client("ccboss")
            page = c.get(f"/delivery/clients/{cid}").data.decode()
            self.assertIn("Pool deck costs in 2026", page)
            self.assertIn('href="https://card-homes.example/blog/pool-deck/" target="_blank" rel="noopener noreferrer"', page)
            self.assertIn(f"/delivery/clients/{cid}/posts/{did}/image", page)
            with mock.patch.object(netsafe, "urlopen", return_value=Resp()) as op:
                r = c.get(f"/delivery/clients/{cid}/posts/{did}/image")
            self.assertEqual((r.status_code, r.mimetype), (200, "image/jpeg"))
            self.assertEqual(op.call_args[0][0].full_url, "https://card-homes.example/img.jpg")  # only the saved URL is fetched
            self.assertTrue(r.headers["Content-Security-Policy"].startswith("default-src 'none'"))
            self.assertEqual(c.get(f"/delivery/clients/{cid + 999}/posts/{did}/image").status_code, 404)  # wrong client
            self.assertEqual(self.client("ccadmin2").get(f"/delivery/clients/{cid}/posts/{did}/image").status_code, 404)


class AlertRows(unittest.TestCase):
    """Change 75: every flag shows a short title, a plain detail and where it gets fixed."""
    def test_titles_and_fix_links(self):
        from webapp.cc import views
        v = views.flag_view({"code": "report_late", "fkey": "report_late", "reason": "Nov report not confirmed sent by the 7th"})
        self.assertEqual((v["title"], v["fix"]), ("Nov report not marked sent", "#t-tasks"))
        v = views.flag_view({"code": "no_task", "fkey": "no_task:gbp_photo", "reason": "x"})
        self.assertEqual(v["title"], "No ClickUp task for GBP photos & videos this month")
        v = views.flag_view({"code": "source_off", "fkey": "source:gbp", "reason": "Google Business Profile not connected: Google refused on quota: API access isn't approved"})
        self.assertEqual((v["title"], v["detail"], v["fix"]), ("Google Business Profile isn't connected", "Waiting for Google to approve API access", "#t-gbp"))
        v = views.flag_view({"code": "approval", "fkey": "approval:t9", "reason": "Waiting on approval, no change for 7 working days: Blog 3"},
                            {"t9": {"url": "https://app.clickup.com/t/t9"}})
        self.assertEqual((v["fix"], v["external"], v["detail"]), ("https://app.clickup.com/t/t9", True, "Blog 3"))
        self.assertEqual(views.flag_view({"code": "scope_missing", "fkey": "scope_missing", "reason": "Scope missing: blogs, report"})["fix"], "#t-setup")
        # each flag knows its platform, for the logo badge
        self.assertEqual(views.flag_view({"code": "no_task", "fkey": "no_task:blog", "reason": "x"})["brand"], "clickup")
        self.assertEqual(views.flag_view({"code": "source_off", "fkey": "source:gsc", "reason": "Search Console not connected: x"})["brand"], "gsc")


class CopySetup(WithApp):
    """Change 77: the setup travels from one copy of the app to another as a file (no secrets, nothing a sync re-reads)."""
    def test_round_trip_export_wipe_import(self):
        import io
        from webapp.cc import transfer, views
        from webapp.cc.sources import gbp
        gbp.save("1//secret-refresh-token", "seo@example.com", "x")  # must never travel
        pid = self.db.x("INSERT INTO cc_people (name, email, roles, clickup_id, active, created_at) VALUES ('Lav T', 'lav.t@example.com', 'seo', 'cu-77', 1, ?)", (time.time(),))
        cid = self.new_client(name="Copy Homes", domain="copy-homes.example")
        self.db.x("UPDATE cc_clients SET clickup_folder='f-777', clickup_space='Growth Package', seo_id=?, package='Growth' WHERE id=?", (pid, cid))
        views.set_scope(cid, "blog", 4, "t")
        views.set_scope(cid, "review_reply", core.EVERY, "t")
        self.db.x("INSERT INTO cc_rules (client_id, kind, pattern, note, active, set_by, set_at) VALUES (?, 'no_prices', NULL, 'No prices', 1, 't', ?)", (cid, time.time()))
        self.db.x("INSERT INTO cc_notes (client_id, kind, body, author, created_at) VALUES (?, 'note', 'Client prefers email', 't', ?)", (cid, time.time()))
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
        self.db.upsert("cc_logos", {"client_id": cid, "content": png, "mime": "image/png", "source_url": None, "how": "auto", "bg": "dark",
                                    "note": "x", "fetched_at": time.time()}, ["client_id"])
        with self.env():
            raw = self.client("ccboss").get("/delivery/setup/export").data
            self.assertEqual(self.client("ccadmin2").get("/delivery/setup/export").status_code, 404)
        self.assertNotIn(b"secret-refresh-token", raw)
        self.assertNotIn(b'"enc"', raw)
        payload = transfer.parse(raw)
        mine = next(c for c in payload["clients"] if c["name"] == "Copy Homes")
        # wipe this client and person, as if importing into a fresh live site
        for t in ("cc_scope", "cc_rules", "cc_notes", "cc_logos", "cc_history"):
            self.db.x(f"DELETE FROM {t} WHERE client_id=?", (cid,))
        self.db.x("DELETE FROM cc_clients WHERE id=?", (cid,))
        self.db.x("DELETE FROM cc_people WHERE id=?", (pid,))
        prev = transfer.preview({**payload, "clients": [mine]})
        self.assertEqual(prev["new_clients"], ["Copy Homes"])
        with self.env():
            c = self.client("ccboss")
            c.post("/delivery/setup", data={"csrf": "t", "action": "preview", "file": (io.BytesIO(json.dumps({**payload, "clients": [mine]}).encode()), "setup.json")},
                   content_type="multipart/form-data")
            self.assertIn("Import this setup", c.get("/delivery/setup").data.decode())
            c.post("/delivery/setup", data={"csrf": "t", "action": "apply"})
        row = self.db.q("SELECT * FROM cc_clients WHERE clickup_folder='f-777'", one=True)
        self.assertEqual((row["name"], row["domain"], row["package"], row["clickup_space"]), ("Copy Homes", "copy-homes.example", "Growth", "Growth Package"))
        owner = self.db.q("SELECT * FROM cc_people WHERE id=?", (row["seo_id"],), one=True)
        self.assertEqual((owner["clickup_id"], owner["email"]), ("cu-77", "lav.t@example.com"))
        sc = {r["item"]: r["qty"] for r in self.db.q("SELECT item, qty FROM cc_scope WHERE client_id=?", (row["id"],))}
        self.assertEqual(sc, {"blog": 4, "review_reply": core.EVERY})
        self.assertEqual(self.db.q("SELECT note FROM cc_rules WHERE client_id=?", (row["id"],), one=True)["note"], "No prices")
        self.assertEqual(self.db.q("SELECT body FROM cc_notes WHERE client_id=?", (row["id"],), one=True)["body"], "Client prefers email")
        lg = self.db.q("SELECT content, bg FROM cc_logos WHERE client_id=?", (row["id"],), one=True)
        self.assertEqual((bytes(lg["content"]), lg["bg"]), (png, "dark"))
        # importing the same file again changes nothing and adds no duplicates
        transfer.apply({**payload, "clients": [mine]}, "t")
        self.assertEqual(self.db.q("SELECT COUNT(*) AS n FROM cc_clients WHERE clickup_folder='f-777'", one=True)["n"], 1)
        self.assertEqual(self.db.q("SELECT COUNT(*) AS n FROM cc_notes WHERE client_id=?", (row["id"],), one=True)["n"], 1)
        gbp.forget()

    def test_rejects_other_files(self):
        from webapp.cc import transfer
        for bad in (b"", b"not json", json.dumps({"kind": "something-else"}).encode(),
                    json.dumps({"kind": transfer.KIND, "version": 99, "clients": [], "people": []}).encode()):
            with self.assertRaises(transfer.BadFile):
                transfer.parse(bad)


class ViewOnly(WithApp):
    """Change 78: owners (COMMAND_CENTER_EMAILS) change things; people granted on the People page can only look and refresh."""
    def test_viewer_sees_everything_but_cannot_change_anything(self):
        cid = self.new_client(name="View Homes", domain="view-homes.example")
        viewer = self.ids["ccadmin2"]
        self.db.x("UPDATE users SET cc_access=1 WHERE id=?", (viewer,))
        try:
            with self.env():
                v, o = self.client("ccadmin2"), self.client("ccboss")
                page = v.get(f"/delivery/clients/{cid}").data.decode()
                self.assertIn("View only", page)
                self.assertIn('data-edit="0"', page)
                self.assertEqual(v.get("/delivery").status_code, 200)
                # changes are refused on the server
                r = v.post(f"/delivery/clients/{cid}/scope", data={"csrf": "t", "blog": "9"})
                self.assertEqual(r.status_code, 302)
                self.assertFalse(self.db.q("SELECT 1 FROM cc_scope WHERE client_id=?", (cid,), one=True))
                v.post("/delivery/people", data={"csrf": "t", "action": "grant", "user_id": self.ids["ccrep"]})
                self.assertEqual(self.db.q("SELECT cc_access FROM users WHERE id=?", (self.ids["ccrep"],), one=True)["cc_access"], 0)
                v.post("/delivery/people", data={"csrf": "t", "action": "revoke", "user_id": viewer})
                self.assertEqual(self.db.q("SELECT cc_access FROM users WHERE id=?", (viewer,), one=True)["cc_access"], 1)
                v.post(f"/delivery/clients/{cid}/edit", data={"csrf": "t", "name": "Renamed", "status": "active"})
                self.assertEqual(self.db.q("SELECT name FROM cc_clients WHERE id=?", (cid,), one=True)["name"], "View Homes")
                self.assertNotIn("accounts.google.com", v.get("/delivery/gbp/connect", headers={"Sec-Fetch-Site": "same-origin"}).headers.get("Location", ""))
                self.assertNotEqual(v.get("/delivery/setup/export").mimetype, "application/json")
                # refreshing data is allowed
                self.assertEqual(v.post(f"/delivery/clients/{cid}/sync", data={"csrf": "t"}).status_code, 302)
                self.assertTrue(self.db.q("SELECT 1 FROM cc_sync WHERE client_id=? AND status='queued'", (cid,), one=True))
                # the owner still can
                o.post(f"/delivery/clients/{cid}/scope", data={"csrf": "t", "blog": "4"})
                self.assertTrue(self.db.q("SELECT 1 FROM cc_scope WHERE client_id=? AND item='blog'", (cid,), one=True))
                self.assertNotIn("View only", o.get(f"/delivery/clients/{cid}").data.decode())
        finally:
            self.db.x("UPDATE users SET cc_access=0 WHERE id=?", (viewer,))


if __name__ == "__main__":
    unittest.main()


class SeveralGoogleAccounts(WithApp):
    """Change 87: Business Profile and Analytics can each hold several Google accounts; every client is read through the
    account that can see it, and a client missing from the main account gets its own connect button."""
    KEYS = {"GBP_OAUTH_CLIENT_ID": "test-client.apps.googleusercontent.com", "GBP_OAUTH_CLIENT_SECRET": "GOCSPX-test"}

    def setUp(self):
        from aeo import kv
        from webapp.cc.sources import ga4, gbp
        for mod in (gbp, ga4):
            for a in mod.ACC.extras():
                mod.forget(a["email"])
            mod.forget()
            mod.forget()
        kv.update("cc_gbp_dir_at", lambda _o: 0, 0)
        kv.update("cc_ga4_dir_at", lambda _o: 0, 0)
        self.db.x("DELETE FROM cc_gbp_locations")
        self.db.x("DELETE FROM cc_ga4_properties")
        self.keys = mock.patch.object(gbp.config, "key", side_effect=lambda n: self.KEYS.get(n))
        self.keys.start()
        self.addCleanup(self.keys.stop)

    def test_each_client_is_read_through_the_account_that_sees_it(self):
        from aeo import kv
        from webapp.cc import gbp_sync
        from webapp.cc.sources import gbp
        gbp.save("1//main-SECRET", "info@example.com", "ccboss@homebuildermarketers.com")
        gbp.save("1//seo-SECRET", "seo@example.com", "ccboss@homebuildermarketers.com", extra=True)
        self.assertEqual(gbp.stored()["email"], "info@example.com")
        self.assertEqual([a["email"] for a in gbp.ACC.extras()], ["seo@example.com"])
        self.assertNotIn("seo-SECRET", json.dumps(kv.get(gbp.KV_KEY + "_extra")))  # encrypted like the main one
        self.assertEqual(gbp._refresh_token(), "1//main-SECRET")
        with gbp.use("seo@example.com"):
            self.assertEqual(gbp._refresh_token(), "1//seo-SECRET")
        self.assertEqual(gbp._refresh_token(), "1//main-SECRET")  # back to the main account after the block

        def accounts(calls=None):
            return [{"name": "accounts/" + ("S" if gbp.ACC.current() == "seo@example.com" else "M")}]

        def locations(account, calls=None):
            main = {"name": "locations/11", "title": "Main Homes", "websiteUri": "https://main-homes.example"}
            if account == "accounts/S":
                return [{"name": "locations/22", "title": "Seo Homes", "websiteUri": "https://seo-homes.example"}, main]
            return [main]
        used = set()

        def record(*a, **k):
            used.add(gbp.ACC.current())
            return {}
        with mock.patch.object(gbp, "accounts", accounts), mock.patch.object(gbp, "locations", locations):
            self.assertEqual(gbp_sync.refresh_directory([0], force=True), 2)
        via = {r["name"]: r["via"] for r in self.db.q("SELECT name, via FROM cc_gbp_locations")}
        self.assertEqual(via, {"locations/11": "info@example.com", "locations/22": "seo@example.com"})  # seen by both: main wins
        cid = self.new_client(name="Seo Homes", domain="seo-homes.example")
        with mock.patch.object(gbp, "daily_metrics", side_effect=record), \
                mock.patch.object(gbp, "reviews", side_effect=lambda *a, **k: (record(), [], None, 0, True)[1:]), \
                mock.patch.object(gbp, "local_posts", side_effect=lambda *a, **k: (record(), [], True)[1:]), \
                mock.patch.object(gbp, "media", side_effect=lambda *a, **k: (record(), [], True)[1:]):
            gbp_sync.sync(dict(self.db.q("SELECT * FROM cc_clients WHERE id=?", (cid,), one=True)), [0])
        self.assertEqual(used, {"seo@example.com"})
        src = self.db.q("SELECT state, detail FROM cc_sources WHERE client_id=? AND source='gbp'", (cid,), one=True)
        self.assertEqual(src["state"], "ok")
        self.assertIn("via seo@example.com", src["detail"])
        gbp.forget()  # the main account goes: the extra one takes its place, nothing is lost
        self.assertEqual(gbp.stored()["email"], "seo@example.com")
        self.assertEqual(gbp.ACC.extras(), [])
        self.assertEqual(gbp._refresh_token(), "1//seo-SECRET")

    def test_a_failing_extra_account_keeps_its_profiles(self):
        from webapp.cc import gbp_sync
        from webapp.cc.sources import gbp
        gbp.save("1//main", "info@example.com", "x")
        gbp.save("1//seo", "seo@example.com", "x", extra=True)
        self.db.x("INSERT INTO cc_gbp_locations (name, account, title, seen_at, via) VALUES ('locations/22', 'accounts/S', 'Seo Homes', 1, 'seo@example.com')")

        def accounts(calls=None):
            if gbp.ACC.current() == "seo@example.com":
                raise gbp.GBPError("Couldn't reach Google")
            return [{"name": "accounts/M"}]
        with mock.patch.object(gbp, "accounts", accounts), mock.patch.object(gbp, "locations", lambda a, calls=None: []):
            gbp_sync.refresh_directory([0], force=True)
        self.assertTrue(self.db.q("SELECT 1 FROM cc_gbp_locations WHERE name='locations/22'", one=True))  # not dropped on a blip

    def test_connect_another_account_from_a_client_page(self):
        from webapp.cc import data, gbp_sync
        from webapp.cc.sources import gbp
        gbp.save("1//main", "info@example.com", "x")
        cid = self.new_client(name="Missing Homes", domain="missing.example")
        data.set_source(cid, "gbp", "not_connected", "No Business Profile for missing.example in any connected Google account")
        with self.env():
            c = self.client("ccboss")
            page = c.get(f"/delivery/clients/{cid}").data.decode()
            self.assertIn("Connect another Google account", page)
            self.assertIn(f"/delivery/gbp/connect?extra=1&amp;client={cid}", page)
            r = c.get(f"/delivery/gbp/connect?extra=1&client={cid}", headers={"Sec-Fetch-Site": "same-origin"})
            self.assertTrue(r.headers["Location"].startswith(gbp.AUTH_URL))
            with c.session_transaction() as s:
                state = s["cc_gbp_oauth"]["state"]
                self.assertEqual((s["cc_gbp_oauth"]["extra"], s["cc_gbp_oauth"]["client"]), (True, cid))
            with mock.patch.object(gbp, "exchange", return_value=("1//seo", "seo@example.com")), \
                    mock.patch.object(gbp_sync, "refresh_directory", return_value=5):
                r = c.get(f"/delivery/gbp/callback?state={state}&code=abc")
            self.assertTrue(r.headers["Location"].endswith(f"/delivery/clients/{cid}#t-setup"))
            self.assertEqual(gbp.stored()["email"], "info@example.com")  # the main account stays
            self.assertEqual([a["email"] for a in gbp.ACC.extras()], ["seo@example.com"])
            page = c.get("/delivery/sync").data.decode()
            self.assertIn("seo@example.com", page)
            self.assertIn("Add another Google account", page)
            with mock.patch.object(gbp, "revoke", return_value=True), mock.patch.object(gbp_sync, "refresh_directory", return_value=1):
                c.post("/delivery/gbp/disconnect", data={"csrf": "t", "email": "seo@example.com"})
            self.assertEqual(gbp.ACC.extras(), [])
            self.assertEqual(gbp.stored()["email"], "info@example.com")  # removing the extra one leaves the main one

    def test_analytics_worker_threads_use_the_right_account(self):
        from webapp.cc import ga4_sync
        from webapp.cc.sources import ga4
        ga4.save("1//main", "info@example.com", "x")
        ga4.save("1//seo", "seo@example.com", "x", extra=True)
        seen = []

        def summaries(calls=None):
            p = "properties/2" if ga4.ACC.current() == "seo@example.com" else "properties/1"
            return [{"account": "accounts/9", "displayName": "Acc", "propertySummaries": [{"property": p, "displayName": p}]}]

        def streams(prop, calls=None):
            seen.append((prop, ga4.ACC.current()))
            return []
        with mock.patch.object(ga4, "account_summaries", summaries), mock.patch.object(ga4, "web_streams", streams):
            ga4_sync.refresh_directory([0], force=True)
        self.assertIn(("properties/2", "seo@example.com"), seen)
        self.assertIn(("properties/1", "info@example.com"), seen)
        via = {r["name"]: r["via"] for r in self.db.q("SELECT name, via FROM cc_ga4_properties")}
        self.assertEqual(via, {"properties/1": "info@example.com", "properties/2": "seo@example.com"})
