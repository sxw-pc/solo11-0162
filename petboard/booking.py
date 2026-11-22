import itertools
from .source import record

# Solver safeguards: fixtures and store workloads stay far below these caps.
MAX_GROUP_OPTIONS = 200
MAX_COMBINATIONS = 50000
MAX_PLANS = 25


def import_data(db, data):
    with db:
        for value in data.get('groups', []):
            if value.get('split_authorized') and value.get('signed_by') != value['owner']:
                raise ValueError('split authorization must be signed by the owner')
            if record(db, value['event_id'], 'booking_group', value):
                db.execute('INSERT INTO booking_group(id,owner,split_authorized,signed_by) VALUES(?,?,?,?)',
                           (value['id'], value['owner'], 1 if value.get('split_authorized') else 0, value['signed_by']))
        for value in data.get('members', []):
            if value['arrival'] >= value['departure']:
                raise ValueError('booking member must have a positive interval')
            if not db.execute('SELECT 1 FROM booking_group WHERE id=?', (value['group_id'],)).fetchone():
                raise ValueError('booking member must reference an existing group')
            if not db.execute('SELECT 1 FROM animal WHERE id=?', (value['animal_id'],)).fetchone():
                raise ValueError('booking member must reference a known animal')
            existing = db.execute('SELECT event_id FROM booking_member WHERE animal_id=?', (value['animal_id'],)).fetchone()
            if existing and existing['event_id'] != value['event_id']:
                raise ValueError('animal already assigned to a booking group')
            if record(db, value['event_id'], 'booking_member', value):
                db.execute('INSERT INTO booking_member(event_id,group_id,animal_id,arrival,departure) VALUES(?,?,?,?,?)',
                           (value['event_id'], value['group_id'], value['animal_id'], value['arrival'], value['departure']))
        for value in data.get('constraints', []):
            _check_constraint(db, value)
            if record(db, value['event_id'], 'booking_constraint', value):
                db.execute('INSERT INTO booking_constraint(event_id,kind,animal_a,animal_b,signed_by,reason) VALUES(?,?,?,?,?,?)',
                           (value['event_id'], value['kind'], value['animal_a'], value['animal_b'], value['signed_by'], value.get('reason', '')))
        for value in data.get('handovers', []):
            if not value.get('actor'):
                raise ValueError('move handover requires a handover actor')
            if not db.execute('SELECT 1 FROM booking_member WHERE animal_id=?', (value['animal_id'],)).fetchone():
                raise ValueError('move handover must reference a booking member')
            if record(db, value['event_id'], 'booking_handover', value):
                db.execute('INSERT INTO booking_handover(event_id,animal_id,at,actor) VALUES(?,?,?,?)',
                           (value['event_id'], value['animal_id'], value['at'], value['actor']))
        return replan(db)


def _check_constraint(db, value):
    kind = value.get('kind')
    if kind not in ('together', 'separate'):
        raise ValueError('constraint kind must be together or separate')
    if value['animal_a'] == value['animal_b']:
        raise ValueError('constraint must reference two different animals')
    if kind == 'separate':
        if value.get('signed_by') != 'veterinarian':
            raise ValueError('separation constraint must be signed by veterinarian')
        return
    owners = {row['owner'] for row in db.execute('SELECT owner FROM animal WHERE id IN (?,?)',
                                                 (value['animal_a'], value['animal_b']))}
    if len(owners) != 1 or value.get('signed_by') not in owners:
        raise ValueError('together constraint must be signed by the common owner')


def replan(db):
    plans = solve(db)
    db.execute('DELETE FROM booking_plan_item')
    db.execute('DELETE FROM booking_plan_group')
    db.execute('DELETE FROM booking_plan')
    for seq, plan in enumerate(plans, 1):
        db.execute('INSERT INTO booking_plan(id,on_time,intact_groups) VALUES(?,?,?)',
                   (seq, plan['on_time'], plan['intact_groups']))
        for row in plan['groups']:
            db.execute('INSERT INTO booking_plan_group(plan_id,group_id,status,detail) VALUES(?,?,?,?)',
                       (seq, row['group_id'], row['status'], row['detail']))
        for row in plan['items']:
            db.execute('INSERT INTO booking_plan_item(plan_id,group_id,animal_id,status,unit_id,starts,ends,detail) VALUES(?,?,?,?,?,?,?,?)',
                       (seq, row['group_id'], row['animal_id'], row['status'], row['unit_id'], row['starts'], row['ends'], row['detail']))
    return plans


def solve(db):
    units = [dict(r) for r in db.execute('SELECT * FROM kennel_unit ORDER BY id')]
    groups = [dict(r) for r in db.execute('SELECT * FROM booking_group ORDER BY id')]
    members = [dict(r) for r in db.execute('SELECT * FROM booking_member ORDER BY event_id')]
    if not units or not groups or not members:
        return []
    placements = [dict(r) for r in db.execute('SELECT * FROM kennel_placement ORDER BY event_id')]
    constraints = [dict(r) for r in db.execute('SELECT * FROM booking_constraint ORDER BY event_id')]
    handovers = [dict(r) for r in db.execute('SELECT * FROM booking_handover ORDER BY event_id')]
    intake = {r['animal_id']: dict(r) for r in db.execute('SELECT animal_id,state,reason FROM intake_decision')}

    points = set()
    for row in placements:
        points.update((row['starts'], row['ends']))
    for row in members:
        points.update((row['arrival'], row['departure']))
    for row in handovers:
        points.add(row['at'])
    points = sorted(points)
    segments = list(zip(points, points[1:]))

    def seg_range(starts, ends):
        return [i for i, (a, b) in enumerate(segments) if a < ends and starts < b]

    unit_index = {u['id']: i for i, u in enumerate(units)}
    # Committed placements keep their seats: free capacity is what remains.
    free = [[u['capacity']] * len(segments) for u in units]
    for row in placements:
        ui = unit_index.get(row['unit_id'])
        if ui is None:
            continue
        for i in seg_range(row['starts'], row['ends']):
            free[ui][i] -= 1

    eligible, excluded = {}, {}
    for row in members:
        animal = row['animal_id']
        state = intake.get(animal)
        if state and state['state'] == 'accepted':
            eligible[animal] = row
        else:
            excluded[animal] = (state['state'], state['reason']) if state else ('missing', 'intake evidence missing')

    hover = {}
    for row in handovers:
        hover.setdefault(row['animal_id'], []).append(row)

    def individual_options(member):
        arr, dep = member['arrival'], member['departure']
        options = []
        for ui, u in enumerate(units):
            if all(free[ui][i] >= 1 for i in seg_range(arr, dep)):
                options.append(((u['id'], arr, dep, ''),))
        for h in hover.get(member['animal_id'], []):
            t = h['at']
            if not arr < t < dep:
                continue
            for ui1, u1 in enumerate(units):
                if not all(free[ui1][i] >= 1 for i in seg_range(arr, t)):
                    continue
                for ui2, u2 in enumerate(units):
                    if u2['id'] == u1['id']:
                        continue
                    if all(free[ui2][i] >= 1 for i in seg_range(t, dep)):
                        options.append(((u1['id'], arr, t, h['actor']), (u2['id'], t, dep, h['actor'])))
        return sorted(set(options))

    group_members = {g['id']: [m for m in members if m['group_id'] == g['id']] for g in groups}
    forced = {}
    group_options = {}
    for g in groups:
        gid = g['id']
        mems = group_members[gid]
        elig = [m for m in mems if m['animal_id'] in eligible]
        if any(m['animal_id'] in excluded for m in mems) and not g['split_authorized']:
            forced[gid] = 'member rejected by intake and owner split authorization missing'
            group_options[gid] = [{}]
            continue
        if not elig:
            forced[gid] = 'no member passed intake qualification'
            group_options[gid] = [{}]
            continue
        indiv = {m['animal_id']: individual_options(m) for m in elig}
        if g['split_authorized']:
            combos = []
            pools = [indiv[m['animal_id']] + [None] for m in elig]
            for picked in itertools.product(*pools):
                combos.append({m['animal_id']: opt for m, opt in zip(elig, picked) if opt})
                if len(combos) >= MAX_GROUP_OPTIONS:
                    break
            group_options[gid] = combos
        elif len(elig) == 1:
            # A lone member has nothing to split; any individual option (including moves) works.
            only = elig[0]['animal_id']
            group_options[gid] = [{only: opt} for opt in indiv[only]] + [{}]
        else:
            # Without written split authorization the group boards together or not at all.
            options = []
            for ui, u in enumerate(units):
                need = [0] * len(segments)
                assign = {}
                ok = True
                for m in elig:
                    single = ((u['id'], m['arrival'], m['departure'], ''),)
                    if single not in indiv[m['animal_id']]:
                        ok = False
                        break
                    assign[m['animal_id']] = single
                    for i in seg_range(m['arrival'], m['departure']):
                        need[i] += 1
                if ok and all(need[i] <= free[ui][i] for i in range(len(segments))):
                    options.append(assign)
            options.append({})
            group_options[gid] = options

    seps = [(c['animal_a'], c['animal_b']) for c in constraints if c['kind'] == 'separate']
    togs = [(c['animal_a'], c['animal_b']) for c in constraints if c['kind'] == 'together']

    def share_unit(opt1, opt2):
        return any(u1 == u2 and s1 < e2 and s2 < e1
                   for u1, s1, e1, _ in opt1 for u2, s2, e2, _ in opt2)

    def cohoused(opt1, opt2):
        return all(u1 == u2 or not (s1 < e2 and s2 < e1)
                   for u1, s1, e1, _ in opt1 for u2, s2, e2, _ in opt2)

    def check(assign):
        used = [[0] * len(segments) for _ in units]
        for opt in assign.values():
            for uid, s, e, _actor in opt:
                ui = unit_index[uid]
                for i in seg_range(s, e):
                    used[ui][i] += 1
                    if used[ui][i] > free[ui][i]:
                        return False
        for x, y in seps:
            if x in assign and y in assign and share_unit(assign[x], assign[y]):
                return False
        for x, y in togs:
            if (x in assign) != (y in assign):
                return False
            if x in assign and not cohoused(assign[x], assign[y]):
                return False
        return True

    def signature(assign):
        return tuple(sorted((a, uid, s, e) for a, opt in sorted(assign.items()) for uid, s, e, _ in opt))

    def intact_count(assign):
        count = 0
        for g in groups:
            animals = [m['animal_id'] for m in group_members[g['id']]]
            if len(animals) < 2:
                continue
            if all(a in assign for a in animals) and all(
                    cohoused(assign[x], assign[y])
                    for i, x in enumerate(animals) for y in animals[i + 1:]):
                count += 1
        return count

    best = {}
    gid_list = [g['id'] for g in groups]
    scanned = 0
    for picked in itertools.product(*(group_options[gid] for gid in gid_list)):
        scanned += 1
        if scanned > MAX_COMBINATIONS:
            break
        assign = {}
        for part in picked:
            assign.update(part)
        if not check(assign):
            continue
        key = (len(assign), intact_count(assign), frozenset(assign))
        sig = signature(assign)
        if key not in best or sig < best[key][0]:
            best[key] = (sig, dict(assign))

    entries = [(key, value) for key, value in best.items()]
    schedulable = set()
    for (_on, _in, sched), _value in entries:
        schedulable.update(sched)

    def dominated(entry):
        (ao, ai, _), _ = entry
        return any(bo >= ao and bi >= ai and (bo > ao or bi > ai) for (bo, bi, _), _ in entries)

    kept = [entry for entry in entries if not dominated(entry)]
    kept.sort(key=lambda entry: (-entry[0][0], -entry[0][1], entry[1][0]))
    kept = kept[:MAX_PLANS]

    plans = []
    for (on_time, intact, _sched), (_sig, assign) in kept:
        items, group_rows = [], []
        for g in groups:
            gid = g['id']
            mems = group_members[gid]
            animals = [m['animal_id'] for m in mems]
            scheduled = [a for a in animals if a in assign]
            together_all = bool(animals) and len(scheduled) == len(animals) and all(
                cohoused(assign[x], assign[y]) for i, x in enumerate(scheduled) for y in scheduled[i + 1:])
            if together_all:
                status, detail = 'full', 'all members boarded and kept together'
            elif scheduled:
                status, detail = 'split', 'split under owner written authorization'
            elif gid in forced:
                status, detail = 'unschedulable', forced[gid]
            elif any(a in schedulable for a in animals):
                status, detail = 'delayed', 'no kennel capacity left for this group in this plan'
            else:
                status, detail = 'unschedulable', 'no feasible plan can board this group'
            group_rows.append(dict(group_id=gid, status=status, detail=detail))
            for m in mems:
                a = m['animal_id']
                if a in assign:
                    for uid, s, e, actor in assign[a]:
                        items.append(dict(group_id=gid, animal_id=a, status='scheduled', unit_id=uid,
                                          starts=s, ends=e,
                                          detail=f'move handover by {actor}' if actor else ''))
                elif a in excluded:
                    st, reason = excluded[a]
                    items.append(dict(group_id=gid, animal_id=a, status='excluded', unit_id=None,
                                      starts=None, ends=None, detail=f'intake {st}: {reason}'))
                elif gid in forced:
                    items.append(dict(group_id=gid, animal_id=a, status='blocked', unit_id=None,
                                      starts=None, ends=None, detail=forced[gid]))
                else:
                    a_status = 'delayed' if a in schedulable else 'blocked'
                    items.append(dict(group_id=gid, animal_id=a, status=a_status, unit_id=None,
                                      starts=None, ends=None,
                                      detail=_explain(m, assign, units, free, seps, togs, seg_range, share_unit)))
        items.sort(key=lambda r: (r['group_id'], r['animal_id'], r['status'] != 'scheduled', r['starts'] or ''))
        plans.append(dict(on_time=on_time, intact_groups=intact, groups=group_rows, items=items))
    return plans


def _explain(member, assign, units, free, seps, togs, seg_range, share_unit):
    a, arr, dep = member['animal_id'], member['arrival'], member['departure']
    for x, y in togs:
        partner = y if x == a else (x if y == a else None)
        if partner and partner not in assign:
            return f'must share a kennel with {partner}, who is not scheduled in this plan'
    reasons = []
    for ui, u in enumerate(units):
        opt = ((u['id'], arr, dep, ''),)
        used = [0] * len(free[ui])
        for other in assign.values():
            for uid, s, e, _actor in other:
                if uid == u['id']:
                    for i in seg_range(s, e):
                        used[i] += 1
        if any(used[i] + 1 > free[ui][i] for i in seg_range(arr, dep)):
            reasons.append(f"{u['id']}: capacity exceeded")
            continue
        blockers = set()
        for x, y in seps:
            if x == a and y in assign and share_unit(opt, assign[y]):
                blockers.add(y)
            if y == a and x in assign and share_unit(opt, assign[x]):
                blockers.add(x)
        if blockers:
            reasons.append(f"{u['id']}: veterinarian separation from {', '.join(sorted(blockers))}")
    return '; '.join(reasons) if reasons else 'no placement option compatible with this plan'
