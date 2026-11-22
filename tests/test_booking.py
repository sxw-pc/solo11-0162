import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from petboard.__main__ import run_import, audit
from petboard.report import report
from petboard.store import connect

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'


def fixture(name):
    return json.loads((FIX / name).read_text(encoding='utf8'))


def feed(db, kind, value):
    return run_import(db, kind, json.dumps(value, ensure_ascii=False).encode())


def scheduled_set(plan):
    return {i['animal_id'] for i in plan['items'] if i['status'] == 'scheduled'}


def placement_map(plan):
    return {i['animal_id']: i['unit_id'] for i in plan['items'] if i['status'] == 'scheduled'}


def group_status(plan):
    return {g['group_id']: g['status'] for g in plan['groups']}


class BookingCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def seed(self):
        feed(self.db, 'intake-import', fixture('booking_intake.json'))
        feed(self.db, 'kennel-import', fixture('booking_kennel.json'))

    def book(self):
        return feed(self.db, 'booking-import', fixture('booking_ok.json'))

    def plans(self):
        return report(self.db)['booking']['plans']

    def assert_capacity(self, plan):
        committed = [dict(r) for r in self.db.execute('SELECT animal_id,unit_id,starts,ends FROM kennel_placement')]
        caps = {r['id']: r['capacity'] for r in self.db.execute('SELECT id,capacity FROM kennel_unit')}
        points = set()
        for row in committed:
            points.update((row['starts'], row['ends']))
        for item in plan['items']:
            if item['starts']:
                points.update((item['starts'], item['ends']))
        points = sorted(points)
        for s, e in zip(points, points[1:]):
            for unit, cap in caps.items():
                used = sum(1 for row in committed if row['unit_id'] == unit and row['starts'] < e and s < row['ends'])
                used += sum(1 for i in plan['items']
                            if i['status'] == 'scheduled' and i['unit_id'] == unit and i['starts'] < e and s < i['ends'])
                self.assertLessEqual(used, cap, f'{unit} over capacity on [{s},{e})')

    def assert_no_double_placement(self, plan):
        per_animal = {}
        for item in plan['items']:
            if item['status'] == 'scheduled':
                per_animal.setdefault(item['animal_id'], []).append((item['starts'], item['ends']))
        for animal, intervals in per_animal.items():
            for x in range(len(intervals)):
                for y in range(x + 1, len(intervals)):
                    overlap = intervals[x][0] < intervals[y][1] and intervals[y][0] < intervals[x][1]
                    self.assertFalse(overlap, f'{animal} placed in two kennels at the same time')

    def test_weekend_booking_pareto_plans(self):
        self.seed()
        returned = self.book()
        plans = self.plans()
        self.assertEqual(len(plans), len(returned))
        by_metrics = {}
        for plan in plans:
            by_metrics.setdefault((plan['on_time'], plan['intact_groups']), []).append(plan)
        self.assertIn((3, 1), by_metrics)
        self.assertIn((4, 0), by_metrics)
        for a in plans:
            for b in plans:
                if a is b:
                    continue
                dominates = (b['on_time'] >= a['on_time'] and b['intact_groups'] >= a['intact_groups']
                             and (b['on_time'] > a['on_time'] or b['intact_groups'] > a['intact_groups']))
                self.assertFalse(dominates, 'dominated plan must not be offered')
        full = next(p for p in by_metrics[(3, 1)] if scheduled_set(p) == {'chip-A', 'chip-B', 'chip-C'})
        self.assertEqual(placement_map(full), {'chip-A': 'K1', 'chip-B': 'K1', 'chip-C': 'K2'})
        self.assertEqual(group_status(full), {'G1': 'full', 'G2': 'full', 'G3': 'delayed'})
        e_item = next(i for i in full['items'] if i['animal_id'] == 'chip-E')
        self.assertEqual(e_item['status'], 'delayed')
        self.assertIn('chip-A', e_item['detail'])
        self.assertIn('K2', e_item['detail'])
        split = next(p for p in by_metrics[(4, 0)])
        self.assertEqual(scheduled_set(split), {'chip-A', 'chip-B', 'chip-C', 'chip-E'})
        self.assertEqual(placement_map(split), {'chip-A': 'K2', 'chip-B': 'K1', 'chip-C': 'K1', 'chip-E': 'K1'})
        self.assertEqual(group_status(split)['G1'], 'split')
        for plan in plans:
            self.assert_capacity(plan)
            self.assert_no_double_placement(plan)
            self.assertNotIn('chip-D', scheduled_set(plan))
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_revocation_withdraws_animal_and_replans(self):
        self.seed()
        self.book()
        feed(self.db, 'intake-import', fixture('booking_revoked.json'))
        self.assertEqual(self.db.execute("SELECT state FROM intake_decision WHERE animal_id='chip-C'").fetchone()[0], 'revoked')
        self.assertNotEqual(self.db.execute("SELECT state FROM kennel_decision WHERE animal_id='chip-C'").fetchone()[0], 'placed')
        plans = self.plans()
        self.assertEqual(len(plans), 1)
        plan = plans[0]
        self.assertEqual((plan['on_time'], plan['intact_groups']), (3, 1))
        self.assertEqual(placement_map(plan), {'chip-A': 'K1', 'chip-B': 'K1', 'chip-E': 'K2'})
        c_item = next(i for i in plan['items'] if i['animal_id'] == 'chip-C')
        self.assertEqual(c_item['status'], 'excluded')
        self.assertIn('revoked', c_item['detail'])
        self.assertEqual(group_status(plan)['G2'], 'unschedulable')
        self.assertEqual(self.db.execute("SELECT unit_id FROM kennel_placement WHERE animal_id='chip-D'").fetchone()[0], 'K2')
        self.assert_capacity(plan)
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_split_authorization_requires_owner_signature(self):
        self.seed()
        bad = fixture('booking_ok.json')
        bad['groups'][0]['signed_by'] = 'owner-b'
        with self.assertRaisesRegex(ValueError, 'split authorization'):
            feed(self.db, 'booking-import', bad)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM booking_group').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM booking_member').fetchone()[0], 0)

    def test_separation_requires_veterinarian_signature(self):
        self.seed()
        bad = fixture('booking_ok.json')
        bad['constraints'][0]['signed_by'] = 'owner-a'
        with self.assertRaisesRegex(ValueError, 'veterinarian'):
            feed(self.db, 'booking-import', bad)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM booking_constraint').fetchone()[0], 0)

    def test_rejected_member_without_split_authorization_blocks_group(self):
        intake = fixture('booking_intake.json')
        intake['animals'] = [a for a in intake['animals'] if a['id'] in ('chip-A', 'chip-B')]
        intake['certificates'] = [c for c in intake['certificates'] if c['animal_id'] in ('chip-A', 'chip-B')]
        intake['certificates'][1]['vaccine_until'] = '2026-11-21'
        feed(self.db, 'intake-import', intake)
        self.assertEqual(self.db.execute("SELECT state FROM intake_decision WHERE animal_id='chip-B'").fetchone()[0], 'rejected')
        feed(self.db, 'kennel-import', {'units': [dict(event_id='u1', id='K1', zone='standard', capacity=3)]})
        feed(self.db, 'booking-import', {
            'groups': [dict(event_id='g1', id='G1', owner='owner-a', split_authorized=False, signed_by='owner-a')],
            'members': [dict(event_id='m1', group_id='G1', animal_id='chip-A', arrival='2026-11-20', departure='2026-11-23'),
                        dict(event_id='m2', group_id='G1', animal_id='chip-B', arrival='2026-11-20', departure='2026-11-23')]})
        plans = self.plans()
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]['on_time'], 0)
        self.assertEqual(group_status(plans[0])['G1'], 'unschedulable')
        items = {i['animal_id']: i for i in plans[0]['items']}
        self.assertEqual(items['chip-B']['status'], 'excluded')
        self.assertEqual(items['chip-A']['status'], 'blocked')
        self.assertIn('split authorization', items['chip-A']['detail'])
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_event_conflict_and_idempotent_replay(self):
        self.seed()
        self.book()
        before = self.plans()
        feed(self.db, 'booking-import', fixture('booking_ok.json'))
        self.assertEqual(self.plans(), before)
        bad = fixture('booking_ok.json')
        bad['constraints'][0]['reason'] = 'tampered'
        with self.assertRaisesRegex(ValueError, 'event_id conflict'):
            feed(self.db, 'booking-import', bad)
        self.assertEqual(self.plans(), before)

    def move_setup(self, handovers):
        feed(self.db, 'intake-import', {
            'animals': [dict(event_id='mv-animal-m', id='chip-M', owner='owner-m', arrival='2026-11-20', departure='2026-11-23', daily_fee=100, service_fee=10),
                        dict(event_id='mv-animal-x', id='chip-X', owner='owner-x', arrival='2026-11-20', departure='2026-11-21', daily_fee=100, service_fee=10),
                        dict(event_id='mv-animal-y', id='chip-Y', owner='owner-y', arrival='2026-11-22', departure='2026-11-23', daily_fee=100, service_fee=10)],
            'certificates': [dict(event_id=f'mv-cert-{a}', animal_id=f'chip-{a}', version=1, chip=f'chip-{a}',
                                  signed_by='veterinarian', vaccine_until='2026-12-31', contraindications=[], condition='healthy')
                             for a in 'MXY']})
        feed(self.db, 'kennel-import', {
            'units': [dict(event_id='mv-unit-k1', id='K1', zone='standard', capacity=1),
                      dict(event_id='mv-unit-k2', id='K2', zone='standard', capacity=1)],
            'placements': [dict(event_id='mv-place-x', animal_id='chip-X', unit_id='K1', starts='2026-11-20', ends='2026-11-21', actor='keeper-a'),
                           dict(event_id='mv-place-y', animal_id='chip-Y', unit_id='K2', starts='2026-11-22', ends='2026-11-23', actor='keeper-a')]})
        booking = {'groups': [dict(event_id='mv-grp', id='GM', owner='owner-m', split_authorized=False, signed_by='owner-m')],
                   'members': [dict(event_id='mv-mem-m', group_id='GM', animal_id='chip-M', arrival='2026-11-20', departure='2026-11-23')]}
        if handovers:
            booking['handovers'] = handovers
        return feed(self.db, 'booking-import', booking)

    def test_mid_stay_move_keeps_capacity_and_names_handover_actor(self):
        self.move_setup([dict(event_id='mv-hover-1', animal_id='chip-M', at='2026-11-22', actor='keeper-b')])
        plans = self.plans()
        self.assertEqual(len(plans), 1)
        segments = [(i['unit_id'], i['starts'], i['ends']) for i in plans[0]['items'] if i['status'] == 'scheduled']
        self.assertEqual(segments, [('K2', '2026-11-20', '2026-11-22'), ('K1', '2026-11-22', '2026-11-23')])
        details = [i['detail'] for i in plans[0]['items'] if i['status'] == 'scheduled']
        self.assertTrue(all('keeper-b' in d for d in details))
        self.assertEqual(group_status(plans[0])['GM'], 'full')
        self.assert_capacity(plans[0])
        self.assert_no_double_placement(plans[0])
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_move_without_handover_actor_is_not_executable(self):
        self.move_setup(None)
        plans = self.plans()
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]['on_time'], 0)
        self.assertEqual(group_status(plans[0])['GM'], 'unschedulable')
        m_item = next(i for i in plans[0]['items'] if i['animal_id'] == 'chip-M')
        self.assertEqual(m_item['status'], 'blocked')

    def together_setup(self, with_constraint):
        feed(self.db, 'intake-import', {
            'animals': [dict(event_id='tg-animal-p', id='chip-P', owner='owner-p', arrival='2026-11-20', departure='2026-11-22', daily_fee=100, service_fee=10),
                        dict(event_id='tg-animal-q', id='chip-Q', owner='owner-p', arrival='2026-11-20', departure='2026-11-22', daily_fee=100, service_fee=10)],
            'certificates': [dict(event_id=f'tg-cert-{a}', animal_id=f'chip-{a}', version=1, chip=f'chip-{a}',
                                  signed_by='veterinarian', vaccine_until='2026-12-31', contraindications=[], condition='healthy')
                             for a in 'PQ']})
        feed(self.db, 'kennel-import', {'units': [dict(event_id='tg-unit-k1', id='K1', zone='standard', capacity=1),
                                                  dict(event_id='tg-unit-k2', id='K2', zone='standard', capacity=1)]})
        booking = {'groups': [dict(event_id='tg-grp', id='G1', owner='owner-p', split_authorized=True, signed_by='owner-p')],
                   'members': [dict(event_id='tg-mem-p', group_id='G1', animal_id='chip-P', arrival='2026-11-20', departure='2026-11-22'),
                               dict(event_id='tg-mem-q', group_id='G1', animal_id='chip-Q', arrival='2026-11-20', departure='2026-11-22')]}
        if with_constraint:
            booking['constraints'] = [dict(event_id='tg-tog-pq', kind='together', animal_a='chip-P', animal_b='chip-Q',
                                           signed_by='owner-p', reason='bonded pair must share')]
        return feed(self.db, 'booking-import', booking)

    def test_together_constraint_is_hard_even_with_split_authorization(self):
        self.together_setup(True)
        plans = self.plans()
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]['on_time'], 0)
        self.assertEqual(group_status(plans[0])['G1'], 'unschedulable')
        p_item = next(i for i in plans[0]['items'] if i['animal_id'] == 'chip-P')
        self.assertEqual(p_item['status'], 'blocked')
        self.assertIn('chip-Q', p_item['detail'])

    def test_split_authorization_allows_split_without_together_constraint(self):
        self.together_setup(False)
        plans = self.plans()
        self.assertEqual(len(plans), 1)
        self.assertEqual((plans[0]['on_time'], plans[0]['intact_groups']), (2, 0))
        placement = placement_map(plans[0])
        self.assertEqual(set(placement), {'chip-P', 'chip-Q'})
        self.assertNotEqual(placement['chip-P'], placement['chip-Q'])
        self.assertEqual(group_status(plans[0])['G1'], 'split')

    def test_public_cli_booking_import_report_and_audit(self):
        dbpath = Path(self.tmp.name) / 'cli.sqlite'
        for command, filename in [('intake-import', 'booking_intake.json'), ('kennel-import', 'booking_kennel.json'),
                                  ('booking-import', 'booking_ok.json')]:
            subprocess.run([sys.executable, '-B', '-m', 'petboard', command, '--db', str(dbpath), '--file', str(FIX / filename)],
                           cwd=ROOT, capture_output=True, text=True, check=True)
        out = subprocess.run([sys.executable, '-B', '-m', 'petboard', 'report', '--db', str(dbpath)],
                             cwd=ROOT, capture_output=True, text=True, check=True)
        plans = json.loads(out.stdout)['booking']['plans']
        self.assertTrue(any(p['on_time'] == 4 for p in plans))
        self.assertTrue(any(p['on_time'] == 3 and p['intact_groups'] == 1 for p in plans))
        out = subprocess.run([sys.executable, '-B', '-m', 'petboard', 'audit', '--db', str(dbpath)],
                             cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(out.stdout)['status'], 'matched')


if __name__ == '__main__':
    unittest.main()
