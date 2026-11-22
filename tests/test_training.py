import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from petboard.store import connect
from petboard import health, training
from petboard.report import report
from petboard.__main__ import audit

ROOT = Path(__file__).parent.parent
FIX = ROOT / 'fixtures'
VIDEO = FIX / 'training_video'


def load(name):
    return json.loads((FIX / name).read_text(encoding='utf8'))


class TrainingChainTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:')
        health.import_data(self.db, load('intake_ok.json'))
        training.import_data(self.db, load('training_evidence_clips.json'))

    def tearDown(self):
        self.db.close()

    def feed(self, name):
        return training.import_data(self.db, load(name))[0]

    def adjudication_by_id(self, result):
        return {item['event_id']: item for item in result['trial_adjudication']}

    def test_legacy_coarse_trial_import_is_retained_but_never_graduates(self):
        result = training.import_data(self.db, load('training_case.json'))[0]
        # The 9/10 coarse session is stored, but without a sealed blind test there is no credential.
        self.assertEqual(len(self.db.execute('SELECT * FROM training_trial').fetchall()), 1)
        self.assertEqual(result['skill_decision']['state'], 'practice')
        self.assertEqual(self.db.execute('SELECT state FROM graduation_decision').fetchone()['state'], 'practice')
        attempt = dict(load('training_case.json')['trials'][0], event_id='trial-101-b',
                       at='2026-11-03T14:00:00', successes=4)
        training.import_data(self.db, {'trials': [attempt]})
        self.assertEqual(self.db.execute('SELECT state FROM graduation_decision').fetchone()['state'], 'practice')
        self.assertEqual(len(report(self.db)['animals'][0]['training_trials']), 2)

    def test_burst_frames_are_one_trial_not_ten(self):
        result = self.feed('training_evidence_sessions.json')
        adjudication = self.adjudication_by_id(result)
        self.assertEqual(adjudication['rt-q9']['status'], 'included')
        for frame in range(2, 11):
            row = adjudication[f'rt-q9-frame-{frame}']
            self.assertFalse(row['eligible'])
            self.assertEqual(row['reason'], 'excluded:burst-frame-not-independent-trial')
        quiet = result['capability_by_setting']['quiet-yard']
        # Nine cued arrivals and one failed unaided attempt; burst frames never inflate the sample.
        self.assertEqual(quiet['cued'], {'successes': 9, 'attempts': 9, 'rate': 1.0})
        self.assertEqual(quiet['independent'], {'successes': 0, 'attempts': 1, 'rate': 0.0})

    def test_repeated_coach_prompt_is_not_an_unaided_recall(self):
        result = self.feed('training_evidence_route_b.json')
        row = self.adjudication_by_id(result)['rt-p7']
        self.assertFalse(row['eligible'])
        self.assertFalse(row['independent'])
        self.assertEqual(row['reason'], 'excluded:repeated-prompt-not-unaided-recall')

    def test_annotation_dispute_stays_pending_and_cannot_mask_failure(self):
        result = self.feed('training_evidence_sessions.json')
        row = self.adjudication_by_id(result)['rt-r8']
        self.assertEqual(row['status'], 'pending')
        self.assertFalse(row['eligible'])
        road = result['capability_by_setting']['open-roadside']
        self.assertEqual(road['pending'], 1)
        # Only seven pre-stop trials are scored: three arrivals. The owner's unaided rate stays visible at 3/7.
        self.assertEqual(road['independent']['successes'], 3)
        self.assertEqual(road['independent']['attempts'], 7)
        self.assertAlmostEqual(road['independent']['rate'], 3 / 7, places=4)

    def test_welfare_stop_excludes_later_trials_and_blocks_road_scope(self):
        result = self.feed('training_evidence_sessions.json')
        adjudication = self.adjudication_by_id(result)
        for event_id in ('rt-r9', 'rt-r10'):
            self.assertEqual(adjudication[event_id]['reason'], 'excluded:trial-after-welfare-stop')
        self.assertEqual(result['welfare_blocked_settings'], ['open-roadside'])
        blocked = {row['scope']: row for row in result['credentials'] if row['state'] == 'blocked'}
        self.assertIn('independent-recall:open-roadside', blocked)
        self.assertIn('cued-recall:open-roadside', blocked)
        self.assertIn('welfare stop recorded at 2026-11-03T09:25:20',
                      blocked['independent-recall:open-roadside']['reason'])
        # No sealed protocol yet -> still practice.
        self.assertEqual(result['skill_decision']['state'], 'practice')

    def test_handler_and_setting_do_not_transfer(self):
        result = self.feed('training_evidence_sessions.json')
        strata = result['capability_by_setting']
        self.assertEqual(set(strata), {'quiet-yard', 'open-roadside'})
        self.assertEqual(strata['quiet-yard']['handlers'], ['coach-a'])
        self.assertEqual(strata['open-roadside']['handlers'], ['owner-a'])
        # Coach's 9 cued successes never appear in the owner's roadside independent rate.
        # Rounded to 4 decimals in the persisted strata.
        self.assertAlmostEqual(strata['open-roadside']['independent']['rate'], round(3 / 7, 4), places=4)

    def test_route_a_issues_only_indoor_cued_recall(self):
        self.feed('training_evidence_sessions.json')
        result = self.feed('training_evidence_route_a.json')
        adjudication = self.adjudication_by_id(result)
        self.assertEqual(adjudication['rt-a9']['reason'], 'excluded:off-camera-cue-detected-on-raw-clip')
        self.assertEqual(adjudication['rt-a10']['reason'], 'excluded:physical-guidance-not-recall')
        evaluations = {e['protocol_id']: e for e in result['protocol_evaluations']}
        evaluation = evaluations['proto-a-indoor-cued']
        self.assertTrue(evaluation['valid'], evaluation['failures'])
        self.assertEqual(evaluation['cue_support'], 'cued')
        self.assertEqual((evaluation['successes'], evaluation['eligible_attempts']), (7, 8))
        self.assertEqual(result['skill_decision']['state'], 'passed')
        graduation = self.db.execute('SELECT * FROM graduation_decision').fetchone()
        self.assertEqual(graduation['state'], 'graduated')
        self.assertEqual(graduation['credential'], 'cued-recall:indoor')
        issued = {row['scope'] for row in result['credentials'] if row['state'] == 'issued'}
        self.assertEqual(issued, {'cued-recall:indoor'})
        # The indoor cued credential must never be read as open-area independent permission.
        self.assertNotIn('independent-recall:open-roadside', issued)

    def test_route_b_issues_enclosed_pen_independent_recall(self):
        self.feed('training_evidence_sessions.json')
        result = self.feed('training_evidence_route_b.json')
        adjudication = self.adjudication_by_id(result)
        self.assertEqual(adjudication['rt-b10']['reason'], 'excluded:repeated-prompt-not-unaided-recall')
        evaluations = {e['protocol_id']: e for e in result['protocol_evaluations']}
        evaluation = evaluations['proto-b-pen-independent']
        self.assertTrue(evaluation['valid'], evaluation['failures'])
        self.assertEqual(evaluation['cue_support'], 'independent')
        self.assertEqual((evaluation['successes'], evaluation['eligible_attempts']), (8, 9))
        self.assertGreaterEqual(evaluation['rate'], 0.8)
        graduation = self.db.execute('SELECT * FROM graduation_decision').fetchone()
        self.assertEqual(graduation['state'], 'graduated')
        self.assertEqual(graduation['credential'], 'independent-recall:enclosed-pen')

    def test_full_ab_routes_keep_distinct_owner_scopes(self):
        self.feed('training_evidence_sessions.json')
        self.feed('training_evidence_route_a.json')
        result = self.feed('training_evidence_route_b.json')
        issued = {row['scope']: row for row in result['credentials'] if row['state'] == 'issued'}
        self.assertEqual(set(issued), {'cued-recall:indoor', 'independent-recall:enclosed-pen'})
        self.assertEqual(issued['cued-recall:indoor']['handler'], 'owner-a')
        self.assertEqual(issued['independent-recall:enclosed-pen']['handler'], 'owner-a')
        graduation = self.db.execute('SELECT * FROM graduation_decision').fetchone()
        # Two safe training routes, two different promise boundaries; nothing grants roadside off-lead recall.
        self.assertNotIn('open-roadside', graduation['credential'])
        blocked = {row['scope'] for row in result['credentials'] if row['state'] == 'blocked'}
        self.assertIn('independent-recall:open-roadside', blocked)

    def test_unsigned_exclusion_blocks_credential_closure(self):
        self.feed('training_evidence_sessions.json')
        data = load('training_evidence_route_a.json')
        data['reviews'][0]['deviations'] = []  # reviewer no longer signs off the two excluded trials
        result = training.import_data(self.db, data)[0]
        evaluation = result['protocol_evaluations'][0]
        self.assertFalse(evaluation['valid'])
        self.assertTrue(any('without signed review deviation' in failure for failure in evaluation['failures']))
        self.assertEqual(self.db.execute('SELECT state FROM graduation_decision').fetchone()['state'], 'practice')

    def test_unsealed_or_unrandomized_protocol_cannot_publish_scope(self):
        self.feed('training_evidence_sessions.json')
        data = load('training_evidence_route_b.json')
        data['protocols'][0]['sealed'] = False
        data['protocols'][0]['randomized'] = False
        result = training.import_data(self.db, data)[0]
        failures = result['protocol_evaluations'][0]['failures']
        self.assertIn('protocol was not sealed before testing began', failures)
        self.assertIn('cue/position order was not randomized; fixed-order leakage cannot be ruled out', failures)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM scope_credential WHERE state='issued'").fetchone()[0], 0)

    def test_observer_who_handles_is_not_independent(self):
        self.feed('training_evidence_sessions.json')
        data = load('training_evidence_route_a.json')
        data['protocols'][0]['independent_observer'] = 'owner-a'
        result = training.import_data(self.db, data)[0]
        self.assertFalse(result['protocol_evaluations'][0]['valid'])
        self.assertIn('independent observer missing or identical to tested owner',
                      result['protocol_evaluations'][0]['failures'])

    def test_welfare_stop_after_registration_keeps_blind_test_within_boundary(self):
        # A protocol registered for the dangerous roadside can never close once chasing triggered a stop.
        self.feed('training_evidence_sessions.json')
        data = {
            'protocols': [{
                'protocol_id': 'proto-road-blocked', 'animal_id': 'chip-101',
                'registered_at': '2026-11-02T08:00:00', 'target_scope': 'independent-recall:open-roadside',
                'setting': 'open-roadside', 'tested_owner': 'owner-a', 'allowed_cues': ['none'],
                'randomized': True, 'independent_observer': 'observer-b', 'min_attempts': 5,
                'pass_threshold': 0.8, 'welfare_stop_conditions': ['car-chasing'], 'sealed': True}],
            'recall_trials': [],
        }
        # Re-capture two roadside trials under this protocol: one before and one after the stop.
        before = next(t for t in load('training_evidence_sessions.json')['recall_trials'] if t['event_id'] == 'rt-r1')
        after = next(t for t in load('training_evidence_sessions.json')['recall_trials'] if t['event_id'] == 'rt-r10')
        before = dict(before, event_id='rt-road-proto-1', protocol_id='proto-road-blocked')
        after = dict(after, event_id='rt-road-proto-2', protocol_id='proto-road-blocked')
        data['recall_trials'] = [before, after]
        result = training.import_data(self.db, data)[0]
        evaluation = result['protocol_evaluations'][0]
        self.assertFalse(evaluation['valid'])
        self.assertTrue(any('welfare stop boundary' in f for f in evaluation['failures']))
        blocked = {row['scope'] for row in result['credentials'] if row['state'] == 'blocked'}
        self.assertIn('independent-recall:open-roadside', blocked)

    def test_training_observations_without_sealed_protocol_never_graduate(self):
        result = self.feed('training_evidence_sessions.json')
        self.assertEqual(result['skill_decision']['state'], 'practice')
        self.assertIn('no sealed credential issued', result['skill_decision']['reason'])
        self.assertFalse(any(c['state'] == 'issued' for c in result['credentials']))
        graduation = self.db.execute('SELECT * FROM graduation_decision').fetchone()
        self.assertEqual(graduation['state'], 'practice')
        self.assertEqual(graduation['credential'], '')

    def test_plain_trials_without_welfare_event_still_require_a_protocol(self):
        # Only the quiet-yard sessions: no welfare stop here, so the missing-protocol branch must show.
        data = load('training_evidence_sessions.json')
        data = {'recall_trials': [t for t in data['recall_trials'] if t['setting'] == 'quiet-yard']}
        result = training.import_data(self.db, data)[0]
        self.assertEqual(result['skill_decision']['state'], 'practice')
        self.assertIn('no pre-registered sealed blind protocol', result['skill_decision']['reason'])

    def test_clip_replay_verification_matches_and_detects_tampering(self):
        checks = {c['clip_id']: c for c in training.verify_clips(self.db, VIDEO)}
        self.assertTrue(all(c['status'] == 'matched' for c in checks.values()))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'recall_quiet_yard.mp4').write_bytes(b'tampered bytes')
            checks = training.verify_clips(self.db, root)
            statuses = {c['clip_id']: c['status'] for c in checks}
            self.assertEqual(statuses['clip-quiet'], 'hash-mismatch')
            self.assertTrue(all(s == 'missing' for cid, s in statuses.items() if cid != 'clip-quiet'))

    def test_report_contains_reviewable_windows_and_full_chain(self):
        self.feed('training_evidence_sessions.json')
        self.feed('training_evidence_route_a.json')
        item = report(self.db)['animals'][0]
        self.assertEqual(len(item['video_clips']), 5)
        trial = next(t for t in item['recall_trials'] if t['event_id'] == 'rt-q1')
        self.assertEqual([trial['window_start_ms'], trial['window_end_ms']], [0, 7000])
        self.assertEqual(trial['annotator'], 'annotator-a')
        self.assertEqual(len(item['blind_protocols']), 1)
        self.assertEqual(item['blind_reviews'][0]['result'], 'valid')
        self.assertEqual(len(item['welfare_events']), 2)
        self.assertTrue(any(c['state'] == 'blocked' for c in item['scope_credentials']))

    def test_import_validation_rejects_bad_cue_and_window(self):
        base = load('training_evidence_sessions.json')['recall_trials'][0]
        bad_cue = dict(base, event_id='rt-bad-cue', cue='shouting')
        with self.assertRaisesRegex(ValueError, 'unsupported cue level'):
            training.import_data(self.db, {'recall_trials': [bad_cue]})
        bad_window = dict(base, event_id='rt-bad-window', window_start_ms=5000, window_end_ms=10)
        with self.assertRaisesRegex(ValueError, 'positive length'):
            training.import_data(self.db, {'recall_trials': [bad_window]})
        self.db.rollback()

    def test_audit_replay_reproduces_every_credential_and_exclusion(self):
        from petboard.__main__ import run_import
        with tempfile.TemporaryDirectory() as tmp:
            dbpath = Path(tmp) / 'audited.sqlite'
            filedb = connect(dbpath)
            run_import(filedb, 'intake-import', (FIX / 'intake_ok.json').read_bytes())
            for name in ('training_evidence_clips.json', 'training_evidence_sessions.json',
                         'training_evidence_route_a.json', 'training_evidence_route_b.json'):
                run_import(filedb, 'training-import', (FIX / name).read_bytes())
            summary = audit(filedb)
            filedb.close()
        self.assertEqual(summary['status'], 'matched')

    def test_cli_verify_and_audit_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbpath = Path(tmp) / 'chain.sqlite'
            commands = [
                ['training-import', '--file', str(FIX / 'intake_ok.json')],
            ]
            # intake goes through its own importer; build the chain with training imports.
            subprocess.run([sys.executable, '-m', 'petboard', 'intake-import', '--db', str(dbpath),
                            '--file', str(FIX / 'intake_ok.json')], cwd=ROOT, capture_output=True, text=True, check=True)
            for name in ('training_evidence_clips.json', 'training_evidence_sessions.json',
                         'training_evidence_route_a.json', 'training_evidence_route_b.json'):
                subprocess.run([sys.executable, '-m', 'petboard', 'training-import', '--db', str(dbpath),
                                '--file', str(FIX / name)], cwd=ROOT, capture_output=True, text=True, check=True)
            verify = subprocess.run([sys.executable, '-m', 'petboard', 'training-verify', '--db', str(dbpath),
                                     '--clip-root', str(VIDEO)], cwd=ROOT, capture_output=True, text=True, check=True)
            self.assertTrue(all(c['status'] == 'matched' for c in json.loads(verify.stdout)['clip_checks']))
            audited = subprocess.run([sys.executable, '-m', 'petboard', 'audit', '--db', str(dbpath)],
                                     cwd=ROOT, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(audited.stdout)['status'], 'matched')


if __name__ == '__main__':
    unittest.main()
