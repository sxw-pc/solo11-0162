import json
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


def intake_for(*pairs):
    animals, certificates = [], []
    for animal_id, shot_id in pairs:
        animals.append({'event_id': f'animal-{animal_id}', 'id': animal_id, 'owner': 'owner-b',
                        'arrival': '2026-11-03', 'departure': '2026-11-06', 'daily_fee': 1200,
                        'service_fee': 150, 'requested_medicine': ''})
        certificates.append({'event_id': f'cert-{animal_id}', 'animal_id': animal_id, 'version': 1,
                             'chip': animal_id, 'signed_by': 'veterinarian', 'vaccine_until': '2026-12-31',
                             'contraindications': [], 'condition': 'healthy', 'shot_id': shot_id})
    return {'animals': animals, 'certificates': certificates}


class VaccineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def vial(self, vial_id):
        row = dict(self.db.execute('SELECT * FROM vial_decision WHERE vial_id=?', (vial_id,)).fetchone())
        row['disposition'] = json.loads(row['disposition'])
        return row

    def shot(self, shot_id):
        return dict(self.db.execute('SELECT * FROM shot_decision WHERE shot_id=?', (shot_id,)).fetchone())

    def intake(self, animal_id):
        return dict(self.db.execute('SELECT * FROM intake_decision WHERE animal_id=?', (animal_id,)).fetchone())

    def route_a(self):
        feed(self.db, 'intake-import', fixture('vaccine_intake.json'))
        feed(self.db, 'vaccine-import', fixture('vaccine_base.json'))
        feed(self.db, 'vaccine-import', fixture('vaccine_route_a.json'))

    def route_b(self):
        feed(self.db, 'intake-import', fixture('vaccine_intake.json'))
        feed(self.db, 'vaccine-import', fixture('vaccine_base.json'))
        feed(self.db, 'vaccine-import', fixture('vaccine_route_b.json'))

    def test_certificates_wait_for_real_vial_evidence(self):
        feed(self.db, 'intake-import', fixture('vaccine_intake.json'))
        for animal_id in ('chip-201', 'chip-202', 'chip-203'):
            self.assertEqual(self.intake(animal_id)['state'], 'review')
            self.assertIn('shot evidence missing', self.intake(animal_id)['reason'])

    def test_route_a_release_then_late_calibration_reopens_intake(self):
        self.route_a()
        vial1 = self.vial('vial-1')
        self.assertEqual((vial1['administered'], vial1['usable'], vial1['pending'], vial1['discarded']), (4, 0, 0, 0))
        self.assertEqual(vial1['state'], 'ok')
        self.assertEqual(self.vial('vial-2')['usable'], 4)
        for animal_id in ('chip-201', 'chip-202', 'chip-203'):
            self.assertEqual(self.intake(animal_id)['state'], 'accepted')
        feed(self.db, 'care-import', {'plans': [{'event_id': 'plan-201', 'animal_id': 'chip-201', 'version': 1,
            'food': 'chicken', 'medicine': '', 'meals': 1, 'approved_by': 'owner'}],
            'actions': [{'event_id': 'meal-201', 'animal_id': 'chip-201', 'plan_id': 'plan-201', 'kind': 'meal',
                         'day': '2026-11-03', 'actor': 'nurse-a', 'witness': 'checker-b', 'food': 'chicken'}]})
        self.assertEqual(dict(self.db.execute("SELECT * FROM care_decision WHERE animal_id='chip-201'").fetchone())['state'], 'fulfilled')
        feed(self.db, 'vaccine-import', fixture('vaccine_late_calibration.json'))
        vial1 = self.vial('vial-1')
        self.assertEqual(vial1['administered'], 4)  # real injections are never erased
        self.assertEqual(vial1['state'], 'review')
        self.assertEqual(self.shot('shot-0')['state'], 'valid')
        for shot_id in ('shot-1', 'shot-2', 'shot-3'):
            self.assertEqual(self.shot(shot_id)['state'], 'review')
            self.assertIn('unreleased temperature anomaly', self.shot(shot_id)['reason'])
        for animal_id in ('chip-201', 'chip-202', 'chip-203'):
            self.assertEqual(self.intake(animal_id)['state'], 'review')
        care = dict(self.db.execute("SELECT * FROM care_decision WHERE animal_id='chip-201'").fetchone())
        self.assertEqual((care['state'], care['reason']), ('blocked', 'animal health intake under review'))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM care_action WHERE animal_id='chip-201'").fetchone()[0], 1)
        states = [r[0] for r in self.db.execute("SELECT state FROM decision_history WHERE animal_id='chip-201' AND subsystem='intake_decision' ORDER BY id")]
        self.assertEqual(states, ['review', 'accepted', 'review'])
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_route_b_disposal_and_substitute_vial_survive_calibration(self):
        self.route_b()
        vial1 = self.vial('vial-1')
        self.assertEqual((vial1['administered'], vial1['discarded'], vial1['usable']), (1, 3, 0))
        discarded = {d['dose_seq']: d['detail'] for d in vial1['disposition'] if d['state'] == 'discarded'}
        self.assertEqual(set(discarded), {2, 3, 4})
        self.assertTrue(all('hazardous-waste-a' in detail for detail in discarded.values()))
        vial2 = self.vial('vial-2')
        self.assertEqual((vial2['administered'], vial2['usable']), (3, 1))
        for animal_id in ('chip-201', 'chip-202', 'chip-203'):
            self.assertEqual(self.intake(animal_id)['state'], 'accepted')
        feed(self.db, 'vaccine-import', fixture('vaccine_late_calibration.json'))
        for animal_id in ('chip-201', 'chip-202', 'chip-203'):
            self.assertEqual(self.intake(animal_id)['state'], 'accepted')
        vial1 = self.vial('vial-1')
        self.assertEqual((vial1['administered'], vial1['discarded']), (1, 3))
        self.assertEqual(self.shot('shot-0')['state'], 'valid')
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_batch_capacity_and_waste_differ_between_routes(self):
        self.route_a()
        batch_a = report(self.db)['batches'][0]
        self.assertEqual((batch_a['usable'], batch_a['administered'], batch_a['discarded']), (4, 4, 0))
        self.db.close()
        self.tmp.cleanup()
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')
        self.route_b()
        batch_b = report(self.db)['batches'][0]
        self.assertEqual((batch_b['usable'], batch_b['administered'], batch_b['discarded']), (1, 4, 3))

    def test_dose_double_use_and_overflow_never_issue_certificate(self):
        feed(self.db, 'intake-import', intake_for(('chip-204', 'shot-4'), ('chip-205', 'shot-5'), ('chip-206', 'shot-6')))
        feed(self.db, 'vaccine-import', fixture('vaccine_base.json'))
        feed(self.db, 'vaccine-import', fixture('vaccine_double_use.json'))
        self.assertEqual(self.shot('shot-4')['state'], 'review')
        self.assertIn('double-used', self.shot('shot-4')['reason'])
        self.assertEqual(self.shot('shot-5')['state'], 'review')
        self.assertEqual(self.shot('shot-6')['state'], 'invalid')
        self.assertIn('exceeds vial nominal doses', self.shot('shot-6')['reason'])
        vial2 = self.vial('vial-2')
        self.assertEqual((vial2['usable'], vial2['pending']), (3, 1))
        self.assertEqual(self.intake('chip-204')['state'], 'review')
        self.assertEqual(self.intake('chip-205')['state'], 'review')
        self.assertEqual(self.intake('chip-206')['state'], 'rejected')

    def test_temperature_packet_gap_blocks_full_course_claim(self):
        feed(self.db, 'intake-import', intake_for(('chip-207', 'shot-7')))
        feed(self.db, 'vaccine-import', fixture('vaccine_base.json'))
        feed(self.db, 'vaccine-import', fixture('vaccine_gap.json'))
        self.assertEqual(self.shot('shot-7')['state'], 'review')
        self.assertIn('temperature packet missing 2026-11-02T08:00..2026-11-03T10:00', self.shot('shot-7')['reason'])
        vial3 = self.vial('vial-3')
        self.assertEqual((vial3['administered'], vial3['pending'], vial3['usable']), (1, 3, 0))
        self.assertEqual(self.intake('chip-207')['state'], 'review')

    def test_open_expiry_timeout_lists_doses_for_review(self):
        feed(self.db, 'intake-import', intake_for(('chip-208', 'shot-8')))
        feed(self.db, 'vaccine-import', fixture('vaccine_base.json'))
        feed(self.db, 'vaccine-import', fixture('vaccine_timeout.json'))
        self.assertEqual(self.shot('shot-8')['state'], 'review')
        self.assertIn('open expiry exceeded', self.shot('shot-8')['reason'])
        vial4 = self.vial('vial-4')
        self.assertEqual((vial4['administered'], vial4['pending'], vial4['usable']), (1, 3, 0))
        self.assertEqual(self.intake('chip-208')['state'], 'review')

    def test_idempotent_replay_and_conflicting_event_id(self):
        value = fixture('vaccine_base.json')
        feed(self.db, 'vaccine-import', value)
        old = self.vial('vial-1')['version']
        evidence = self.db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]
        feed(self.db, 'vaccine-import', value)
        self.assertEqual(self.vial('vial-1')['version'], old)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0], evidence)
        value = fixture('vaccine_base.json')
        value['shots'][0]['dose_seq'] = 2
        with self.assertRaisesRegex(ValueError, 'event_id conflict'):
            feed(self.db, 'vaccine-import', value)
        self.assertEqual(self.shot('shot-0')['dose_seq'], 1)

    def test_audit_rebuilds_vial_attribution_and_quantities(self):
        self.route_a()
        feed(self.db, 'vaccine-import', fixture('vaccine_late_calibration.json'))
        self.assertEqual(audit(self.db)['status'], 'matched')
        with self.db:
            self.db.execute("UPDATE vial_decision SET administered=9 WHERE vial_id='vial-1'")
        with self.assertRaisesRegex(ValueError, 'disagree'):
            audit(self.db)


if __name__ == '__main__':
    unittest.main()
