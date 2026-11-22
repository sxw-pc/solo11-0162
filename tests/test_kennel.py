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

    def animal(self, event_id, chip):
        return dict(event_id=event_id, id=chip, owner='owner-1', arrival='2026-11-01', departure='2026-11-03',
                    daily_fee=10, service_fee=1)

    def certificate(self, event_id, chip):
        return dict(event_id=event_id, animal_id=chip, version=1, chip=chip, signed_by='veterinarian',
                    vaccine_until='2026-12-01', contraindications=[], condition='healthy')

    def test_capacity_counts_simultaneous_occupancy_not_whole_interval_overlap(self):
        db = connect(':memory:')
        try:
            chips = ['chip-1', 'chip-2', 'chip-3']
            health.import_data(db, {'animals': [self.animal(f'a{i}', c) for i, c in enumerate(chips, 1)],
                                    'certificates': [self.certificate(f'c{i}', c) for i, c in enumerate(chips, 1)]})
            result = kennel.import_data(db, {
                'units': [dict(event_id='u1', id='K1', zone='standard', capacity=2)],
                'placements': [dict(event_id='p1', animal_id='chip-1', unit_id='K1', starts='2026-11-01', ends='2026-11-02', actor='keeper'),
                               dict(event_id='p2', animal_id='chip-2', unit_id='K1', starts='2026-11-02', ends='2026-11-03', actor='keeper'),
                               dict(event_id='p3', animal_id='chip-3', unit_id='K1', starts='2026-11-01', ends='2026-11-03', actor='keeper')]})
            states = {row['animal_id']: row['state'] for row in db.execute('SELECT animal_id,state FROM kennel_decision')}
            # chip-3 overlaps two different neighbours but never more than two animals at once.
            self.assertEqual(states, {'chip-1': 'placed', 'chip-2': 'placed', 'chip-3': 'placed'})
            self.assertEqual(len(result), 3)
        finally:
            db.close()

    def test_same_animal_cannot_occupy_two_units_at_once(self):
        db = connect(':memory:')
        try:
            health.import_data(db, {'animals': [self.animal('a1', 'chip-1')],
                                    'certificates': [self.certificate('c1', 'chip-1')]})
            kennel.import_data(db, {
                'units': [dict(event_id='u1', id='K1', zone='standard', capacity=2),
                          dict(event_id='u2', id='K2', zone='standard', capacity=2)],
                'placements': [dict(event_id='p1', animal_id='chip-1', unit_id='K1', starts='2026-11-01', ends='2026-11-02', actor='keeper')]})
            kennel.import_data(db, {'placements': [dict(event_id='p2', animal_id='chip-1', unit_id='K2',
                                                        starts='2026-11-01', ends='2026-11-02', actor='keeper')]})
            row = db.execute("SELECT state,reason FROM kennel_decision WHERE animal_id='chip-1'").fetchone()
            self.assertEqual(row['state'], 'conflict')
            self.assertIn('two kennels', row['reason'])
        finally:
            db.close()
