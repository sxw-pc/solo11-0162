from pathlib import Path
import unittest

from petboard.__main__ import audit, run_import
from petboard.store import connect

FIX = Path(__file__).resolve().parents[1] / 'fixtures'


class ClaimHistoryCase(unittest.TestCase):
    def test_reported_claim_rows_replay(self):
        db = connect(':memory:')
        try:
            run_import(db, 'claim-history-import', (FIX / 'claim_history_ok.json').read_bytes())
            self.assertEqual(db.execute("SELECT SUM(paid_cents) FROM reserve_claim WHERE incurred_month='2026-08'").fetchone()[0], 50000)
            self.assertEqual(audit(db)['status'], 'matched')
        finally:
            db.close()
