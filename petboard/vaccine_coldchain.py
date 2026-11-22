import json
from datetime import datetime, timedelta
from .source import record


def record_events(db, data):
    """Record cold chain supply, temperature, opening, review and disposal events.

    Returns the set of vial ids whose ledger must be recomputed. Caller owns the
    transaction and the downstream recompute (see dose_ledger.import_data).
    """
    touched = set()
    for value in data.get('batches', []):
        if record(db, value['event_id'], 'vaccine_batch', value):
            db.execute('INSERT INTO vaccine_batch(event_id,batch_id,manufacturer,vaccine,min_temp,max_temp,open_expiry_hours,received_at,signed_by) VALUES(?,?,?,?,?,?,?,?,?)',
                       (value['event_id'], value['batch_id'], value['manufacturer'], value['vaccine'],
                        value['min_temp'], value['max_temp'], value['open_expiry_hours'], value['received_at'], value['signed_by']))
    for value in data.get('vials', []):
        if record(db, value['event_id'], 'vaccine_vial', value):
            db.execute('INSERT INTO vaccine_vial(event_id,vial_id,batch_id,nominal_doses,stored_from) VALUES(?,?,?,?,?)',
                       (value['event_id'], value['vial_id'], value['batch_id'], value['nominal_doses'], value['stored_from']))
        touched.add(value['vial_id'])
    for value in data.get('temperature_packets', []):
        if record(db, value['event_id'], 'temperature_packet', value):
            db.execute('INSERT INTO temperature_packet(event_id,vial_id,logger_id,from_ts,to_ts,readings) VALUES(?,?,?,?,?,?)',
                       (value['event_id'], value['vial_id'], value['logger_id'], value['from_ts'], value['to_ts'],
                        json.dumps(value['readings'], ensure_ascii=False)))
        touched.add(value['vial_id'])
    for value in data.get('calibrations', []):
        if record(db, value['event_id'], 'logger_calibration', value):
            db.execute('INSERT INTO logger_calibration(event_id,logger_id,offset,effective_from,effective_to,signed_by,at) VALUES(?,?,?,?,?,?,?)',
                       (value['event_id'], value['logger_id'], value['offset'], value['effective_from'], value['effective_to'],
                        value['signed_by'], value['at']))
        for row in db.execute('SELECT DISTINCT vial_id FROM temperature_packet WHERE logger_id=?', (value['logger_id'],)):
            touched.add(row['vial_id'])
    for value in data.get('openings', []):
        if record(db, value['event_id'], 'vial_opening', value):
            db.execute('INSERT INTO vial_opening(event_id,vial_id,opened_at,opened_by,witness,retention_sample) VALUES(?,?,?,?,?,?)',
                       (value['event_id'], value['vial_id'], value['opened_at'], value['opened_by'],
                        value.get('witness'), value.get('retention_sample')))
        touched.add(value['vial_id'])
    for value in data.get('stability_reviews', []):
        if record(db, value['event_id'], 'stability_review', value):
            db.execute('INSERT INTO stability_review(event_id,vial_id,anomaly_from,anomaly_to,covers_max_temp,covers_min_temp,covers_max_hours,retest,retention_sample,signed_by,manufacturer_signed_by,at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (value['event_id'], value['vial_id'], value['anomaly_from'], value['anomaly_to'],
                        value.get('covers_max_temp'), value.get('covers_min_temp'), value['covers_max_hours'],
                        value.get('retest', ''), value.get('retention_sample'), value['signed_by'],
                        value['manufacturer_signed_by'], value['at']))
        touched.add(value['vial_id'])
    for value in data.get('disposals', []):
        if record(db, value['event_id'], 'dose_disposal', value):
            db.execute('INSERT INTO dose_disposal(event_id,vial_id,dose_seqs,weight_grams,handed_to,witness,at) VALUES(?,?,?,?,?,?,?)',
                       (value['event_id'], value['vial_id'], json.dumps(value['dose_seqs']), value['weight_grams'],
                        value['handed_to'], value.get('witness'), value['at']))
        touched.add(value['vial_id'])
    return touched


def calibrated_readings(db, vial_id):
    """Yield (ts, temp, packet_id) with the latest covering logger calibration applied."""
    packets = db.execute('SELECT * FROM temperature_packet WHERE vial_id=? ORDER BY from_ts,event_id', (vial_id,)).fetchall()
    readings = []
    for packet in packets:
        calibrations = db.execute('SELECT * FROM logger_calibration WHERE logger_id=? ORDER BY at,event_id',
                                  (packet['logger_id'],)).fetchall()
        for ts, raw in json.loads(packet['readings']):
            offset = 0.0
            for cal in calibrations:
                if cal['effective_from'] <= ts <= cal['effective_to']:
                    offset = cal['offset']
            readings.append((ts, raw + offset, packet['event_id']))
    readings.sort()
    return readings


def anomaly_windows(db, vial, batch, as_of):
    """Group consecutive out-of-range calibrated readings into excursion windows."""
    windows = []
    current = None
    for ts, temp, _packet in calibrated_readings(db, vial['vial_id']):
        if ts > as_of:
            continue
        if temp > batch['max_temp'] or temp < batch['min_temp']:
            if current is None:
                current = {'from': ts, 'to': ts, 'max_temp': temp, 'min_temp': temp}
            else:
                current['to'] = ts
                current['max_temp'] = max(current['max_temp'], temp)
                current['min_temp'] = min(current['min_temp'], temp)
        elif current is not None:
            windows.append(current)
            current = None
    if current is not None:
        windows.append(current)
    for window in windows:
        hours = (datetime.fromisoformat(window['to']) - datetime.fromisoformat(window['from'])).total_seconds() / 3600
        window['hours'] = max(0.0, hours)
    return windows


def coverage_gaps(db, vial, as_of):
    """Intervals in [stored_from, as_of] not covered by any raw temperature packet."""
    packets = db.execute('SELECT from_ts,to_ts FROM temperature_packet WHERE vial_id=? ORDER BY from_ts',
                         (vial['vial_id'],)).fetchall()
    gaps = []
    cursor = vial['stored_from']
    for packet in packets:
        if packet['to_ts'] < cursor:
            continue
        if packet['from_ts'] > cursor:
            gaps.append((cursor, packet['from_ts']))
        cursor = max(cursor, packet['to_ts'])
        if cursor >= as_of:
            return gaps
    if cursor < as_of:
        gaps.append((cursor, as_of))
    return gaps


def review_covers(review, window):
    """A stability release only counts when vet and manufacturer co-signed a passed
    retention-sample retest that bounds the calibrated excursion actually observed."""
    return (review['signed_by'] == 'veterinarian'
            and review['manufacturer_signed_by'] and review['manufacturer_signed_by'] != 'veterinarian'
            and review['retest'] == 'passed'
            and review['anomaly_from'] <= window['from'] and review['anomaly_to'] >= window['to']
            and (review['covers_max_temp'] is None or review['covers_max_temp'] >= window['max_temp'])
            and (review['covers_min_temp'] is None or review['covers_min_temp'] <= window['min_temp'])
            and review['covers_max_hours'] >= window['hours'])


def chain_problems(db, vial, batch, as_of):
    """Cold chain defects of a vial as of a moment: packet gaps, unreleased
    calibrated anomalies and open-vial timeout. A last good reading never
    stands in for full-course coverage."""
    problems = []
    for gap_from, gap_to in coverage_gaps(db, vial, as_of):
        problems.append(f'temperature packet missing {gap_from}..{gap_to}')
    reviews = db.execute('SELECT * FROM stability_review WHERE vial_id=? ORDER BY at,event_id', (vial['vial_id'],)).fetchall()
    for window in anomaly_windows(db, vial, batch, as_of):
        if not any(review_covers(review, window) for review in reviews):
            problems.append(f"unreleased temperature anomaly {window['from']}..{window['to']} max {window['max_temp']}")
    opening = db.execute('SELECT opened_at FROM vial_opening WHERE vial_id=? ORDER BY opened_at LIMIT 1', (vial['vial_id'],)).fetchone()
    if opening:
        deadline = datetime.fromisoformat(opening['opened_at']) + timedelta(hours=batch['open_expiry_hours'])
        if datetime.fromisoformat(as_of) > deadline:
            problems.append(f"open expiry exceeded since {deadline.isoformat()}")
    return problems


def vial_as_of(db, vial_id, stored_from):
    """Latest known fact time for a vial; remaining doses are judged against it."""
    moments = [stored_from]
    for sql in ["SELECT MAX(to_ts) FROM temperature_packet WHERE vial_id=?",
                "SELECT MAX(opened_at) FROM vial_opening WHERE vial_id=?",
                "SELECT MAX(at) FROM vaccination_shot WHERE vial_id=?",
                "SELECT MAX(at) FROM dose_disposal WHERE vial_id=?"]:
        value = db.execute(sql, (vial_id,)).fetchone()[0]
        if value:
            moments.append(value)
    return max(moments)
