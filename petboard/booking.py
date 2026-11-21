from .source import record


def decide(db, day):
    slot = db.execute('SELECT capacity FROM booking_slot WHERE day=?', (day,)).fetchone()
    capacity = slot['capacity'] if slot else 0
    booked = db.execute("SELECT COUNT(*) FROM booking_intent WHERE day=? AND status='confirmed'", (day,)).fetchone()[0]
    state = 'sold_out' if capacity and booked >= capacity else ('available' if capacity else 'pending')
    db.execute('INSERT INTO booking_decision(day,capacity,booked,state) VALUES(?,?,?,?) '
               'ON CONFLICT(day) DO UPDATE SET capacity=excluded.capacity,booked=excluded.booked,state=excluded.state',
               (day, capacity, booked, state))
    from .funnel import decide as funnel_decide
    funnel_decide(db, day)
    return dict(db.execute('SELECT * FROM booking_decision WHERE day=?', (day,)).fetchone())


def import_data(db, data):
    touched = set()
    with db:
        for row in data.get('slots', []):
            if row['capacity'] < 1:
                raise ValueError('capacity must be positive')
            if record(db, row['event_id'], 'booking_slot', row):
                db.execute('INSERT INTO booking_slot(day,capacity) VALUES(?,?) '
                           'ON CONFLICT(day) DO UPDATE SET capacity=excluded.capacity',
                           (row['day'], row['capacity']))
            touched.add(row['day'])
        for row in data.get('intents', []):
            if row['status'] not in ('confirmed', 'cancelled'):
                raise ValueError('invalid booking status')
            if record(db, row['event_id'], 'booking_intent', row):
                db.execute('INSERT INTO booking_intent(event_id,day,visitor,price,status) VALUES(?,?,?,?,?)',
                           (row['event_id'], row['day'], row['visitor'], row['price'], row['status']))
            touched.add(row['day'])
        return [decide(db, day) for day in sorted(touched)]
