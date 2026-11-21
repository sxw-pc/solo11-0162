import json
import unittest

from petboard.store import connect
from petboard import health, kennel


class KennelCase(unittest.TestCase):
    def test_accepted_animal_occupies_unit_and_revocation_blocks_it(self):
        db = connect(':memory:')
        try:
            health.import_data(db, {'animals': [dict(event_id='a1', id='chip-1', owner='owner-1', arrival='2026-11-01', departure='2026-11-03', daily_fee=10, service_fee=1)],
                                    'certificates': [dict(event_id='c1', animal_id='chip-1', version=1, chip='chip-1', signed_by='veterinarian', vaccine_until='2026-12-01', contraindications=[], condition='healthy')]})
            result = kennel.import_data(db, {'units': [dict(event_id='u1', id='K1', zone='standard', capacity=1)],
                                             'placements': [dict(event_id='p1', animal_id='chip-1', unit_id='K1', starts='2026-11-01', ends='2026-11-03', actor='keeper')]})
            self.assertEqual(result[0]['state'], 'placed')
            health.import_data(db, {'revocations': [dict(event_id='r1', certificate_id='c1', animal_id='chip-1', at='2026-11-02', signed_by='veterinarian', reason='positive test')]})
            self.assertEqual(db.execute("SELECT state FROM kennel_decision WHERE animal_id='chip-1'").fetchone()[0], 'blocked')
        finally:
            db.close()
