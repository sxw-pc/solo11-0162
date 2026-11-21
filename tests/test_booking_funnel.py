import json
import unittest
from pathlib import Path
from petboard.__main__ import run_import, audit
from petboard.store import connect
from petboard.report import report

ROOT = Path(__file__).resolve().parents[1]


class BookingFunnelTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:')

    def tearDown(self):
        self.db.close()

    def test_booking_changes_demand_observation(self):
        booking = (ROOT / 'fixtures' / 'booking_ok.json').read_bytes()
        views = (ROOT / 'fixtures' / 'funnel_ok.json').read_bytes()
        run_import(self.db, 'booking-import', booking)
        run_import(self.db, 'funnel-import', views)
        day = report(self.db)['demand_days'][0]
        self.assertEqual((day['views'], day['sold_out_views'], day['booked']), (3, 2, 2))
        self.assertEqual(report(self.db)['booking_days'][0]['state'], 'sold_out')
        self.assertEqual(audit(self.db)['status'], 'matched')
        run_import(self.db, 'funnel-import', views)
        self.assertEqual(report(self.db)['demand_days'][0]['views'], 3)

    def test_invalid_page_state_rejected(self):
        bad = {'views': [{'event_id': 'bad-1', 'day': '2026-11-07', 'visitor': 'v-6', 'observed': 'unknown'}]}
        with self.assertRaises(ValueError):
            run_import(self.db, 'funnel-import', json.dumps(bad).encode())


if __name__ == '__main__':
    unittest.main()
