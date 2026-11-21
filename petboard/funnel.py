from .source import record


def decide(db, day):
    booking = db.execute('SELECT booked FROM booking_decision WHERE day=?', (day,)).fetchone()
    views = db.execute('SELECT observed FROM page_view WHERE day=?', (day,)).fetchall()
    sold = sum(row['observed'] == 'sold_out' for row in views)
    db.execute('INSERT INTO demand_decision(day,views,sold_out_views,booked,state) VALUES(?,?,?,?,?) '
               'ON CONFLICT(day) DO UPDATE SET views=excluded.views,sold_out_views=excluded.sold_out_views,'
               'booked=excluded.booked,state=excluded.state',
               (day, len(views), sold, booking['booked'] if booking else 0, 'observed'))
    return dict(db.execute('SELECT * FROM demand_decision WHERE day=?', (day,)).fetchone())


def import_data(db, data):
    touched = set()
    with db:
        for row in data.get('views', []):
            if row['observed'] not in ('available', 'sold_out'):
                raise ValueError('invalid page availability')
            if record(db, row['event_id'], 'page_view', row):
                db.execute('INSERT INTO page_view(event_id,day,visitor,observed) VALUES(?,?,?,?)',
                           (row['event_id'], row['day'], row['visitor'], row['observed']))
            touched.add(row['day'])
        return [decide(db, day) for day in sorted(touched)]
