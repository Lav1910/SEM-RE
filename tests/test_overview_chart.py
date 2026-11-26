"""Change 89: a small score isn't shown as 0, and the 1M chart view shows each audit day."""
import os as _os, tempfile as _tf
_os.environ.setdefault("HBM_KV_DIR", _tf.mkdtemp(prefix="hbm-kv-test-"))
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("HBM_DATA_DIR", tempfile.mkdtemp(prefix="hbm-test-"))

from webapp import views  # noqa: E402

DAY = 86400


class SmallScores(unittest.TestCase):
    def test_named_only_on_low_demand_questions_is_not_zero(self):
        self.assertEqual(views._score(60, 23000), 0.3)   # 6 mentions, all on ~10-search questions
        self.assertEqual(views._score(1, 1_000_000), 0.1)  # never below 0.1 once named at all
        self.assertEqual(views._score(0, 23000), 0)       # never named: a true 0
        self.assertEqual(views._score(46, 100), 46)       # whole numbers as before
        self.assertIsNone(views._score(0, 0))             # nothing measured


class DailyChart(unittest.TestCase):
    def hist(self, days_ago_list):
        t0 = 1_800_000_000
        return [{"t": t0 - d * DAY, "date": f"day-{d}", "month": "2027-01", "mentions": 10 - d, "citations": 1, "pages": 1,
                 "audience": 5, "score": 20 + d} for d in days_ago_list]

    def test_1m_uses_the_audit_days_of_the_last_30_days(self):
        c = views.chart({}, self.hist([40, 20, 5, 0]))
        self.assertEqual(c["days"], ["day-20", "day-5", "day-0"])  # 40 days ago is outside the 1M window
        self.assertEqual(c["daily"]["main"][0]["values"], [-10, 5, 10])
        self.assertEqual(c["daily"]["vis"][0]["values"], [40, 25, 20])
        self.assertEqual(len(c["months"]), 1)  # the monthly view still rolls these into one month

    def test_no_history_means_no_days(self):
        c = views.chart({}, [])
        self.assertEqual(c["days"], [])


if __name__ == "__main__":
    unittest.main()
