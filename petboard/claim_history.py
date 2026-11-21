from .source import record


def import_data(db, data):
    results = []
    with db:
        for row in data.get('exposures', []):
            if int(row['animal_days']) < 0 or len(row['month']) != 7:
                raise ValueError('month and nonnegative animal days required')
            if record(db, row['event_id'], 'reserve_exposure', row):
                db.execute('INSERT INTO reserve_exposure(event_id,month,animal_days) VALUES(?,?,?)',
                           (row['event_id'], row['month'], int(row['animal_days'])))
            results.append({'event_id': row['event_id'], 'state': 'recorded'})
        for row in data.get('claims', []):
            if row['incurred_month'] > row['reported_month'] or int(row['paid_cents']) < 0:
                raise ValueError('valid claim months and nonnegative payment required')
            if record(db, row['event_id'], 'reserve_claim', row):
                db.execute('INSERT INTO reserve_claim(event_id,incurred_month,reported_month,paid_cents) VALUES(?,?,?,?)',
                           (row['event_id'], row['incurred_month'], row['reported_month'], int(row['paid_cents'])))
            results.append({'event_id': row['event_id'], 'state': 'recorded'})
    return results
