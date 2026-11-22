"""Build deterministic local recall-training fixtures for petboard.

Run from the repository root:

    python tools/build_training_fixtures.py

The raw "video" clips are synthetic, offline, replayable byte streams; real video
files are not required. Every clip carries a sidecar manifest and RFID/location
logs so a reviewer can replay the exact time window behind each recall trial.
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VIDEO_DIR = ROOT / 'fixtures' / 'training_video'
FIX = ROOT / 'fixtures'

CLIP_SPECS = [
    ('clip-quiet',  'recall_quiet_yard.mp4',    '2026-11-02T14:00:00'),
    ('clip-road',   'recall_open_roadside.mp4', '2026-11-03T09:00:00'),
    ('clip-indoor', 'recall_indoor_blind.mp4',  '2026-11-05T10:00:00'),
    ('clip-pen-a',  'recall_enclosed_pen.mp4',  '2026-11-06T10:00:00'),
    ('clip-pen-b',  'recall_enclosed_blind.mp4', '2026-11-07T10:00:00'),
]


def build_clips():
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    clips = []
    for index, (clip_id, filename, recorded_at) in enumerate(CLIP_SPECS):
        path = VIDEO_DIR / filename
        frames = []
        for frame in range(15):
            frames.append({
                'frame': frame,
                'offset_ms': frame * 8000,
                'rfid': f'rfid-chip-101-gate-{frame % 3}',
                'location': f'sector-{index}-{frame % 4}',
            })
        rfid_log = [f'rfid-chip-101 gate={frame % 3} t={frame * 8000}ms' for frame in range(15)]
        location_log = [f'sector-{index}-{frame % 4} t={frame * 8000}ms' for frame in range(15)]
        manifest = {
            'clip_id': clip_id,
            'frame_count': 15,
            'frame_ms': 8000,
            'frames': frames,
            'note': 'synthetic replayable index; burst frames share one recall attempt and are not independent trials',
        }
        payload = (
            f'SYNTHETIC-PETBOARD-CLIP\nclip_id={clip_id}\nrecorded_at={recorded_at}\n'
            f'frame_count=15\n' + '\n'.join(rfid_log) + '\n' + '\n'.join(location_log) + '\n'
            + json.dumps(manifest, ensure_ascii=False, sort_keys=True)
        ).encode()
        path.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        sidecar = {
            'clip_id': clip_id,
            'file': filename,
            'sha256': digest,
            'recorded_at': recorded_at,
            'duration_ms': 120000,
            'frame_count': 15,
            'rfid_log': rfid_log,
            'location_log': location_log,
            'manifest': manifest,
        }
        (VIDEO_DIR / filename.replace('.mp4', '.sidecar.json')).write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=2), encoding='utf-8')
        clips.append({
            'clip_id': clip_id,
            'path': filename,
            'sha256': digest,
            'recorded_at': recorded_at,
            'duration_ms': 120000,
            'frame_count': 15,
            'rfid_log': rfid_log,
            'location_log': location_log,
            'manifest': manifest,
        })
    return clips


def trial(event_id, clip, session, frame, handler, cue, setting, distraction, arrived,
          annotator='annotator-a', protocol_id=None, stress=None, note=None,
          off_camera_cue=False, physical_guidance=False, repeated_prompt=False,
          annotation_state='confirmed', burst_id=None, duplicate_of=None,
          day='2026-11-02', start_clock='14:00:00', offset_s=None):
    start_ms = frame * 8000
    if offset_s is None:
        hh, mm, ss = (int(part) for part in start_clock.split(':'))
        base = hh * 3600 + mm * 60 + ss + frame * 90
    else:
        hh, mm, ss = (int(part) for part in start_clock.split(':'))
        base = hh * 3600 + mm * 60 + ss + offset_s
    end = base + 7
    clock = lambda total: f'{total // 3600:02d}:{(total // 60) % 60:02d}:{total % 60:02d}'
    return {
        'event_id': event_id,
        'animal_id': 'chip-101',
        'clip_id': clip,
        'session_id': session,
        'protocol_id': protocol_id,
        'started_at': f'{day}T{clock(base)}',
        'ended_at': f'{day}T{clock(end)}',
        'window_start_ms': start_ms,
        'window_end_ms': start_ms + 7000,
        'handler': handler,
        'cue': cue,
        'setting': setting,
        'distraction': distraction,
        'off_camera_cue': off_camera_cue,
        'physical_guidance': physical_guidance,
        'repeated_prompt': repeated_prompt,
        'arrived': int(arrived),
        'stress_actions': stress or [],
        'annotator': annotator,
        'annotation_state': annotation_state,
        'note': note,
        'burst_id': burst_id,
        'duplicate_of': duplicate_of,
    }


def build_recall_sessions(clips):
    quiet, road = 'clip-quiet', 'clip-road'
    indoor, pen_a, pen_b = 'clip-indoor', 'clip-pen-a', 'clip-pen-b'

    quiet_trials = [
        trial('rt-q1', quiet, 'sess-quiet', 0, 'coach-a', 'voice+gesture', 'quiet-yard', 'low', True, start_clock='14:00:00'),
        trial('rt-q2', quiet, 'sess-quiet', 1, 'coach-a', 'gesture', 'quiet-yard', 'low', True, start_clock='14:00:00'),
        trial('rt-q3', quiet, 'sess-quiet', 2, 'coach-a', 'voice+gesture', 'quiet-yard', 'low', True, start_clock='14:00:00'),
        trial('rt-q4', quiet, 'sess-quiet', 3, 'coach-a', 'gesture', 'quiet-yard', 'low', True, start_clock='14:00:00'),
        trial('rt-q5', quiet, 'sess-quiet', 4, 'coach-a', 'voice+gesture', 'quiet-yard', 'low', True, start_clock='14:00:00'),
        trial('rt-q6', quiet, 'sess-quiet', 5, 'coach-a', 'gesture', 'quiet-yard', 'low', True, start_clock='14:00:00'),
        trial('rt-q7', quiet, 'sess-quiet', 6, 'coach-a', 'voice+gesture', 'quiet-yard', 'medium', True, start_clock='14:00:00'),
        trial('rt-q8', quiet, 'sess-quiet', 7, 'coach-a', 'gesture', 'quiet-yard', 'medium', True, start_clock='14:00:00'),
        # Burst capture: ten consecutive frames of ONE physical recall, only rt-q9 is an independent trial.
        trial('rt-q9', quiet, 'sess-quiet', 8, 'coach-a', 'voice+gesture', 'quiet-yard', 'medium', True, burst_id='burst-1', start_clock='14:00:00'),
        *(trial(f'rt-q9-frame-{n}', quiet, 'sess-quiet', 8, 'coach-a', 'voice+gesture', 'quiet-yard', 'medium', True,
                burst_id='burst-1', duplicate_of='rt-q9', start_clock='14:00:00',
                note=f'burst frame {n}/10 of the same attempt, not a separate trial') for n in range(2, 11)),
        trial('rt-q10', quiet, 'sess-quiet', 9, 'coach-a', 'none', 'quiet-yard', 'medium', False,
              stress=['lip-licking', 'look-away'], start_clock='14:00:00',
              note='single unaided attempt fails; cannot be hidden behind the 9/10 coached rate'),
    ]

    def mk_road(event_id, frame, arrived, offset_s, stress=None, **kw):
        return trial(event_id, road, 'sess-road', frame, 'owner-a', 'none', 'open-roadside', 'high', arrived,
                     day='2026-11-03', start_clock='09:00:00', offset_s=offset_s, stress=stress, **kw)

    road_trials = [
        mk_road('rt-r1', 0, True, 0),
        mk_road('rt-r2', 1, True, 120),
        mk_road('rt-r3', 2, True, 240),
        mk_road('rt-r4', 3, False, 360, stress=['car-chasing', 'lunging'],
             note='first car chase; welfare stop declared after this attempt'),
        mk_road('rt-r5', 4, False, 600, stress=['pinned-ears', 'tail-tuck']),
        mk_road('rt-r6', 5, False, 840, stress=['panting', 'avoidance']),
        mk_road('rt-r7', 6, False, 1080, stress=['look-away']),
        mk_road('rt-r8', 7, False, 1320, annotation_state='disputed',
             note='annotators disagree whether the head turn counts as arrival; held pending, never scored'),
        mk_road('rt-r9', 8, False, 1620, stress=['car-chasing', 'lunging'],
             note='second car chase after stop boundary; attempt must not have been forced'),
        mk_road('rt-r10', 9, False, 2820, stress=['freezing'],
             note='attempt started after welfare stop; excluded, animal must not be pushed for more samples'),
    ]
    welfare_events = [
        {'event_id': 'welfare-stop-1', 'animal_id': 'chip-101', 'setting': 'open-roadside',
         'session_id': 'sess-road', 'protocol_id': None, 'at': '2026-11-03T09:25:20',
         'kind': 'car-chasing', 'actor': 'observer-b', 'detail': 'lunged toward moving traffic; stop blind tests in this setting',
         'stopped': True},
        {'event_id': 'welfare-stop-2', 'animal_id': 'chip-101', 'setting': 'open-roadside',
         'session_id': 'sess-road', 'protocol_id': None, 'at': '2026-11-03T09:45:00',
         'kind': 'sustained-stress', 'actor': 'observer-b',
         'detail': 'continued avoidance and freezing after the stop; owner route must leave the dangerous road',
         'stopped': True},
    ]

    # Route A: owner indoor cued blind test under a pre-approved gesture protocol.
    def mk_indoor(event_id, frame, cue, arrived, **kw):
        return trial(event_id, indoor, 'sess-indoor-blind', frame, 'owner-a', cue, 'indoor', kw.pop('distraction', 'medium'), arrived,
                     protocol_id='proto-a-indoor-cued', day='2026-11-05', start_clock='10:00:00', **kw)

    indoor_trials = [
        mk_indoor('rt-a1', 0, 'gesture', True, distraction='low'),
        mk_indoor('rt-a2', 1, 'gesture', True, distraction='low'),
        mk_indoor('rt-a3', 2, 'voice+gesture', True, distraction='low'),
        mk_indoor('rt-a4', 3, 'gesture', True, distraction='low'),
        mk_indoor('rt-a5', 4, 'voice+gesture', True),
        mk_indoor('rt-a6', 5, 'gesture', True),
        mk_indoor('rt-a7', 6, 'gesture', True),
        mk_indoor('rt-a8', 7, 'gesture', False),
        mk_indoor('rt-a9', 8, 'gesture', True, off_camera_cue=True,
               note='raw-clip review catches a coach hand signal outside the camera angle'),
        mk_indoor('rt-a10', 9, 'voice', False, physical_guidance=True,
               note='physical repositioning used; not a recall'),
    ]
    proto_a = {
        'protocol_id': 'proto-a-indoor-cued', 'animal_id': 'chip-101',
        'registered_at': '2026-11-04T16:00:00', 'target_scope': 'cued-recall:indoor',
        'setting': 'indoor', 'tested_owner': 'owner-a',
        'allowed_cues': ['voice', 'gesture', 'voice+gesture'], 'randomized': True,
        'independent_observer': 'observer-b', 'min_attempts': 8, 'pass_threshold': 0.8,
        'welfare_stop_conditions': ['car-chasing', 'sustained-stress', 'freezing', 'lunging'],
        'sealed': True,
    }
    review_a = {
        'event_id': 'review-a-1', 'protocol_id': 'proto-a-indoor-cued', 'animal_id': 'chip-101',
        'reviewed_at': '2026-11-05T12:00:00', 'reviewer': 'observer-b',
        'off_camera_found': 'rt-a9', 'duplicates_found': 'none',
        'deviations': ['rt-a9', 'rt-a10'], 'result': 'valid',
        'note': 'rt-a9 off-camera cue and rt-a10 physical guidance excluded; remaining eight trials are independent observations under approved cues',
    }

    # Route B: enclosed-pen adaptation first, then an owner unaided blind test.
    def mk_pen(event_id, frame, cue, arrived, **kw):
        return trial(event_id, pen_a, 'sess-pen-adapt', frame, 'coach-a', cue, 'enclosed-pen', kw.pop('distraction', 'medium'), arrived,
                     day='2026-11-06', start_clock='10:00:00', **kw)

    pen_trials = [
        mk_pen('rt-p1', 0, 'none', True),
        mk_pen('rt-p2', 1, 'none', True),
        mk_pen('rt-p3', 2, 'none', True, distraction='high'),
        mk_pen('rt-p4', 3, 'none', False, distraction='high', stress=['look-away']),
        mk_pen('rt-p5', 4, 'none', True, distraction='high'),
        mk_pen('rt-p6', 5, 'none', True, distraction='high'),
        mk_pen('rt-p7', 6, 'voice', True, distraction='high', repeated_prompt=True,
            note='coach repeated the cue three times; training observation only, never an unaided trial'),
        mk_pen('rt-p8', 7, 'none', False, distraction='high'),
    ]

    def mk_blind(event_id, frame, cue, arrived, **kw):
        return trial(event_id, pen_b, 'sess-pen-blind', frame, 'owner-a', cue, 'enclosed-pen', 'high', arrived,
                     protocol_id='proto-b-pen-independent', day='2026-11-07', start_clock='10:00:00', **kw)

    pen_blind = [
        mk_blind('rt-b1', 0, 'none', True),
        mk_blind('rt-b2', 1, 'none', True),
        mk_blind('rt-b3', 2, 'none', True),
        mk_blind('rt-b4', 3, 'none', True),
        mk_blind('rt-b5', 4, 'none', False),
        mk_blind('rt-b6', 5, 'none', True),
        mk_blind('rt-b7', 6, 'none', True),
        mk_blind('rt-b8', 7, 'none', True),
        mk_blind('rt-b9', 8, 'none', True),
        mk_blind('rt-b10', 9, 'gesture', False, repeated_prompt=True,
              note='repeated gesture prompts before arrival; excluded, not an unaided recall'),
    ]
    proto_b = {
        'protocol_id': 'proto-b-pen-independent', 'animal_id': 'chip-101',
        'registered_at': '2026-11-06T08:00:00', 'target_scope': 'independent-recall:enclosed-pen',
        'setting': 'enclosed-pen', 'tested_owner': 'owner-a',
        'allowed_cues': ['none'], 'randomized': True,
        'independent_observer': 'observer-c', 'min_attempts': 8, 'pass_threshold': 0.8,
        'welfare_stop_conditions': ['car-chasing', 'sustained-stress', 'freezing', 'lunging'],
        'sealed': True,
    }
    review_b = {
        'event_id': 'review-b-1', 'protocol_id': 'proto-b-pen-independent', 'animal_id': 'chip-101',
        'reviewed_at': '2026-11-07T12:00:00', 'reviewer': 'observer-c',
        'off_camera_found': 'none', 'duplicates_found': 'none',
        'deviations': ['rt-b10'], 'result': 'valid',
        'note': 'rt-b10 repeated gesture prompts excluded; nine unaided trials remain with eight arrivals',
    }

    return {
        'quiet_trials': quiet_trials,
        'road_trials': road_trials,
        'welfare_events': welfare_events,
        'indoor_trials': indoor_trials,
        'proto_a': proto_a,
        'review_a': review_a,
        'pen_trials': pen_trials,
        'pen_blind': pen_blind,
        'proto_b': proto_b,
        'review_b': review_b,
    }


def write_json(name, payload):
    (FIX / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main():
    clips = build_clips()
    data = build_recall_sessions(clips)
    write_json('training_evidence_clips.json', {'clips': clips})
    write_json('training_evidence_sessions.json', {
        'recall_trials': data['quiet_trials'] + data['road_trials'],
        'welfare_events': data['welfare_events'],
    })
    write_json('training_evidence_route_a.json', {
        'protocols': [data['proto_a']],
        'recall_trials': data['indoor_trials'],
        'reviews': [data['review_a']],
    })
    write_json('training_evidence_route_b.json', {
        'recall_trials': data['pen_trials'] + data['pen_blind'],
        'protocols': [data['proto_b']],
        'reviews': [data['review_b']],
    })
    print('wrote clips to', VIDEO_DIR)
    print('wrote fixtures to', FIX)


if __name__ == '__main__':
    main()
