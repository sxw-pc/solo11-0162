"""售罄实验与未成交需求识别子系统。

只记录带同意、带时刻、可重放的真实采集证据：价签快照、访客身份、页面曝光、
预签实验分配、定向邀约、固定价格预授权响应、撤回与实际成单。售罄页面不产生
价格拒绝样本；同一临时空房最多兑现一笔订单。报表区分已证需求下界、尚未检验
访客与不可识别上界，样本不足时给出“无法判定”。
"""
import json
import os
from html.parser import HTMLParser

from .source import record

OBSERVED_STATES = ('available', 'sold_out', 'pending')
RESPONSES = ('signed', 'withdrawn', 'declined')
PLAN_KINDS = ('waitlist', 'instant')
BASELINE = 'none'
MIN_SAMPLE = 8


class PageSnapshotParser(HTMLParser):
    """解析本地合成网页，提取价签快照属性并检查可购买按钮。"""

    def __init__(self):
        super().__init__()
        self.attrs = {}
        self.actions = []

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        for key in ('data-day', 'data-state', 'data-price', 'data-sellable-nights',
                    'data-snapshot-at', 'data-bookable'):
            if key in data:
                self.attrs[key] = data[key]
        if 'data-action' in data:
            self.actions.append(data['data-action'])
        if 'data-price' in data and 'data-price' not in self.attrs:
            self.attrs['data-price'] = data['data-price']


def parse_snapshot_file(path):
    """从本地合成 HTML 读出快照行；售罄页出现立即下单按钮即拒绝。"""
    with open(path, encoding='utf-8') as handle:
        text = handle.read()
    parser = PageSnapshotParser()
    parser.feed(text)
    a = parser.attrs
    required = ('data-day', 'data-state', 'data-price', 'data-sellable-nights',
                'data-snapshot-at', 'data-bookable')
    missing = [key for key in required if key not in a]
    if missing:
        raise ValueError(f'page snapshot missing attributes: {missing}')
    state = a['data-state']
    if state not in OBSERVED_STATES:
        raise ValueError('invalid page state in snapshot html')
    bookable = a['data-bookable'] == '1'
    sellable = int(a['data-sellable-nights'])
    if bookable != (state == 'available' and sellable > 0):
        raise ValueError('page bookable flag disagrees with state and sellable nights')
    if state == 'sold_out' and 'book_now' in parser.actions:
        raise ValueError('sold_out page must not render an immediate book-now button')
    if state == 'available' and 'book_now' not in parser.actions:
        raise ValueError('available page must render an immediate book-now button')
    return {'day': a['data-day'], 'at': a['data-snapshot-at'], 'price': int(a['data-price']),
            'sellable_nights': sellable, 'page_state': state, 'bookable': int(bookable),
            'book_now_button': int('book_now' in parser.actions),
            'page_file': os.path.basename(path)}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _page_root(data):
    root = data.get('page_root')
    return root if root and os.path.isdir(root) else None


def _verify_html(row, root):
    if not root:
        return
    parsed = parse_snapshot_file(os.path.join(root, row['page_file']))
    comparable = {k: parsed[k] for k in ('day', 'at', 'price', 'sellable_nights',
                                         'page_state', 'bookable', 'page_file')}
    given = {k: row[k] for k in comparable}
    _require(comparable == given, f"snapshot json disagrees with page html: {row['event_id']}")


def _stage(db, event_id, kind, row):
    """先做证据冲突检测：异内容重放立即拒绝；幂等重放返回 False 跳过写入。"""
    return record(db, event_id, kind, row)


def import_data(db, data):
    _require(not data.get('simulation_only'),
             '潜在意愿仿真输入不是真实采集证据，不能通过 offer-import 入库')
    touched = set()
    root = _page_root(data)
    with db:
        for row in data.get('snapshots', []):
            is_new = _stage(db, row['event_id'], 'price_snapshot', row)
            _require(row['page_state'] in OBSERVED_STATES, 'invalid snapshot page state')
            _require(row['price'] > 0 and row['sellable_nights'] >= 0, 'invalid snapshot price or nights')
            bookable = row['page_state'] == 'available' and row['sellable_nights'] > 0
            _require(int(row['bookable']) == int(bookable), 'snapshot bookable flag inconsistent')
            if row['page_state'] == 'sold_out':
                _require(not row.get('book_now_button'), 'sold_out page cannot offer book-now')
            _verify_html(row, root)
            if is_new:
                db.execute('INSERT INTO price_snapshot(event_id,day,at,price,sellable_nights,page_state,bookable,page_file) '
                           'VALUES(?,?,?,?,?,?,?,?)',
                           (row['event_id'], row['day'], row['at'], row['price'], row['sellable_nights'],
                            row['page_state'], int(bookable), row['page_file']))
            touched.add(row['day'])
        for row in data.get('identities', []):
            value = {'event_id': row['event_id'], 'visitor': row['visitor'],
                     'consented_at': row['consented_at'], 'scope': row.get('scope', 'stockout-experiment')}
            is_new = _stage(db, row['event_id'], 'visitor_identity', value)
            _require(row.get('consent'), 'visitor consent required before identity recording')
            if is_new:
                db.execute('INSERT INTO visitor_identity(event_id,visitor,consented_at,scope) VALUES(?,?,?,?)',
                           (value['event_id'], value['visitor'], value['consented_at'], value['scope']))
        for row in data.get('exposures', []):
            is_new = _stage(db, row['event_id'], 'page_exposure', row)
            identity = db.execute('SELECT 1 FROM visitor_identity WHERE visitor=?', (row['visitor'],)).fetchone()
            _require(identity, f"exposure without consented identity: {row['visitor']}")
            snapshot = db.execute('SELECT * FROM price_snapshot WHERE event_id=?', (row['snapshot_id'],)).fetchone()
            _require(snapshot, 'exposure must cite an existing price snapshot')
            _require(snapshot['day'] == row['day'], 'exposure day must match snapshot day')
            _require(row['observed'] == snapshot['page_state'],
                     '错误曝光：曝光状态必须与当时页面快照一致，禁止倒写旧访客机会')
            opportunity = 'none' if snapshot['page_state'] == 'sold_out' else 'book_now'
            _require(row.get('opportunity', opportunity) == opportunity, 'exposure opportunity disagrees with snapshot')
            if is_new:
                db.execute('INSERT INTO page_exposure(event_id,day,visitor,at,snapshot_id,observed,opportunity) '
                           'VALUES(?,?,?,?,?,?,?)',
                           (row['event_id'], row['day'], row['visitor'], row['at'],
                            row['snapshot_id'], row['observed'], opportunity))
            touched.add(row['day'])
        for row in data.get('plans', []):
            is_new = _stage(db, row['event_id'], 'experiment_plan', row)
            _require(row['plan_kind'] in PLAN_KINDS, 'invalid experiment plan kind')
            _require(row['budget'] > 0 and row['valid_hours'] > 0, 'plan budget and validity must be positive')
            clash = db.execute('SELECT experiment FROM experiment_plan WHERE day=?', (row['day'],)).fetchone()
            _require(not clash or clash['experiment'] == row['experiment'],
                     '同一房晚只能执行一份真实采集方案，禁止拼合未执行的全量实验')
            if row['plan_kind'] == 'instant':
                _require(not row.get('hold_room'), 'instant offer plan cannot hold the room')
            if is_new:
                db.execute('INSERT INTO experiment_plan(event_id,experiment,day,plan_kind,signed_at,budget,hold_room,auto_convert,valid_hours) '
                           'VALUES(?,?,?,?,?,?,?,?,?)',
                           (row['event_id'], row['experiment'], row['day'], row['plan_kind'], row['signed_at'],
                            row['budget'], int(bool(row.get('hold_room'))), int(bool(row.get('auto_convert'))),
                            row['valid_hours']))
            touched.add(row['day'])
        for row in data.get('assignments', []):
            is_new = _stage(db, row['event_id'], 'experiment_assignment', row)
            plan = db.execute('SELECT * FROM experiment_plan WHERE experiment=?', (row['experiment'],)).fetchone()
            _require(plan, 'assignment must cite an existing experiment plan')
            _require(db.execute('SELECT 1 FROM visitor_identity WHERE visitor=?', (row['visitor'],)).fetchone(),
                     'assignment requires consented visitor identity')
            _require(row['assigned_at'] >= plan['signed_at'], 'assignment cannot predate plan signing')
            if is_new:
                db.execute('INSERT INTO experiment_assignment(event_id,experiment,visitor,assigned_at,cohort,sampling_token) '
                           'VALUES(?,?,?,?,?,?)',
                           (row['event_id'], row['experiment'], row['visitor'], row['assigned_at'],
                            row['cohort'], row['sampling_token']))
        for row in data.get('offers', []):
            is_new = _stage(db, row['event_id'], 'offer', row)
            plan = db.execute('SELECT * FROM experiment_plan WHERE experiment=?', (row['experiment'],)).fetchone()
            _require(plan, 'offer must cite an existing experiment plan')
            _require(plan['day'] == row['day'], 'offer day must match plan day')
            _require(row['kind'] == plan['plan_kind'], 'offer kind must match plan kind')
            _require(db.execute('SELECT 1 FROM experiment_assignment WHERE experiment=? AND visitor=?',
                                (row['experiment'], row['visitor'])).fetchone(),
                     'offer requires a pre-signed experiment assignment')
            seen_price = db.execute('SELECT ps.price FROM page_exposure pe JOIN price_snapshot ps ON ps.event_id=pe.snapshot_id '
                                    'WHERE pe.visitor=? AND pe.day=?', (row['visitor'], row['day'])).fetchall()
            _require(seen_price and all(p['price'] == row['price'] for p in seen_price),
                     'offer price is fixed and must equal the price the visitor actually saw')
            vacancy = db.execute("SELECT 1 FROM booking_intent WHERE event_id=? AND day=? AND status='cancelled'",
                                 (row['vacancy_id'], row['day'])).fetchone()
            _require(vacancy, 'offer must reference the real vacancy opened by a signed cancellation')
            _require(row['valid_to'] > row['valid_from'], 'offer validity window invalid')
            window_hours = _hours(row['valid_from'], row['valid_to'])
            _require(window_hours <= plan['valid_hours'], 'offer window exceeds pre-approved plan validity')
            _require(int(bool(row.get('auto_convert'))) == plan['auto_convert'],
                     'waitlist auto-conversion term must match the signed plan')
            if is_new:
                db.execute('INSERT INTO offer(event_id,experiment,visitor,day,price,kind,valid_from,valid_to,auto_convert,vacancy_id) '
                           'VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (row['event_id'], row['experiment'], row['visitor'], row['day'], row['price'], row['kind'],
                            row['valid_from'], row['valid_to'], int(bool(row.get('auto_convert'))), row['vacancy_id']))
            touched.add(row['day'])
        for row in data.get('responses', []):
            is_new = _stage(db, row['event_id'], 'offer_response', row)
            offer = db.execute('SELECT * FROM offer WHERE event_id=?', (row['offer_id'],)).fetchone()
            _require(offer, 'response must cite an existing offer')
            _require(row['visitor'] == offer['visitor'], 'response visitor must match offer visitor')
            _require(row['response'] in RESPONSES, 'invalid offer response')
            _require(row['price'] == offer['price'], 'pre-authorization is fixed-price and cannot renegotiate')
            _require(offer['valid_from'] <= row['at'] <= offer['valid_to'], 'response outside offer validity window')
            if row['response'] == 'withdrawn':
                signed = db.execute("SELECT 1 FROM offer_response WHERE offer_id=? AND response='signed'",
                                    (row['offer_id'],)).fetchone()
                _require(signed, 'withdrawal requires a prior signed pre-authorization')
            if is_new:
                db.execute('INSERT INTO offer_response(event_id,offer_id,visitor,at,response,price) VALUES(?,?,?,?,?,?)',
                           (row['event_id'], row['offer_id'], row['visitor'], row['at'], row['response'], row['price']))
            touched.add(offer['day'])
        for row in data.get('conversions', []):
            is_new = _stage(db, row['event_id'], 'demand_conversion', row)
            offer = db.execute('SELECT * FROM offer WHERE event_id=?', (row['offer_id'],)).fetchone()
            _require(offer, 'conversion must cite an existing offer')
            plan = db.execute('SELECT * FROM experiment_plan WHERE experiment=?', (offer['experiment'],)).fetchone()
            _require(not plan['hold_room'], '保留给迟到应急入住的房间当天不得转单兑现')
            _require(row['vacancy_id'] == offer['vacancy_id'], 'conversion must fulfill the offer vacancy')
            _require(row['price'] == offer['price'] and row['visitor'] == offer['visitor'],
                     'conversion must match fixed offer price and visitor')
            _require(offer['valid_from'] <= row['at'] <= offer['valid_to'], 'conversion outside offer validity window')
            latest = db.execute('SELECT response FROM offer_response WHERE offer_id=? ORDER BY at DESC,event_id DESC LIMIT 1',
                                (row['offer_id'],)).fetchone()
            _require(latest and latest['response'] == 'signed', 'only an active signed pre-authorization can convert')
            _require(db.execute('SELECT 1 FROM page_exposure WHERE visitor=? AND day=? AND observed=?',
                                (row['visitor'], row['day'], 'sold_out')).fetchone(),
                     'converting visitor must have a real sold-out exposure on record')
            if is_new:
                try:
                    db.execute('INSERT INTO demand_conversion(event_id,offer_id,vacancy_id,experiment,visitor,day,price,at) '
                               'VALUES(?,?,?,?,?,?,?,?)',
                               (row['event_id'], row['offer_id'], row['vacancy_id'], offer['experiment'],
                                row['visitor'], row['day'], row['price'], row['at']))
                except Exception:
                    raise ValueError('同一临时空房只能兑现一笔订单（同房双兑现被拒绝）')
            touched.add(row['day'])
        results = [decide(db, day) for day in sorted(touched)]
        return results


def _hours(start, end):
    from datetime import datetime
    delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    return delta.total_seconds() / 3600


def _latest_response_map(db, experiment):
    """每个邀约的最终响应：signed / withdrawn / declined。"""
    result = {}
    offers = db.execute('SELECT event_id,visitor FROM offer WHERE experiment=?', (experiment,)).fetchall()
    for offer in offers:
        row = db.execute('SELECT response FROM offer_response WHERE offer_id=? ORDER BY at DESC,event_id DESC LIMIT 1',
                         (offer['event_id'],)).fetchone()
        result[offer['event_id']] = (offer['visitor'], row['response'] if row else None)
    return result


def decide(db, day, experiment=None):
    experiments = [r['experiment'] for r in db.execute(
        'SELECT DISTINCT experiment FROM experiment_plan WHERE day=? ORDER BY experiment', (day,))]
    if experiment is not None:
        experiments = [experiment]
    as_of_rows = [r[0] for r in db.execute(
        'SELECT at FROM price_snapshot WHERE day=? UNION ALL SELECT at FROM page_exposure WHERE day=? '
        'UNION ALL SELECT at FROM offer_response WHERE visitor IN (SELECT visitor FROM offer WHERE day=?) '
        'UNION ALL SELECT at FROM demand_conversion WHERE day=?', (day, day, day, day)).fetchall()]
    as_of = max(as_of_rows, default=None)
    exposed = {r['visitor'] for r in db.execute(
        'SELECT DISTINCT visitor FROM page_exposure WHERE day=? AND observed=?', (day, 'sold_out'))}
    total_views = db.execute('SELECT COUNT(*) FROM page_exposure WHERE day=?', (day,)).fetchone()[0]
    sold_views = db.execute('SELECT COUNT(*) FROM page_exposure WHERE day=? AND observed=?',
                            (day, 'sold_out')).fetchone()[0]
    source_files = [dict(r) for r in db.execute(
        "SELECT kind,sha FROM file_journal WHERE kind='offer-import' ORDER BY id")]
    decisions = []
    names = [BASELINE] + experiments
    for name in names:
        if name == BASELINE:
            offered, active_signed, declined, withdrawn = set(), set(), set(), set()
            conversions = set()
            plan_row = None
        else:
            plan_row = db.execute('SELECT * FROM experiment_plan WHERE experiment=? AND day=?', (name, day)).fetchone()
            offered = {r['visitor'] for r in db.execute(
                'SELECT DISTINCT visitor FROM offer WHERE experiment=? AND day=?', (name, day))}
            latest = _latest_response_map(db, name)
            active_signed = {visitor for _offer, (visitor, resp) in latest.items() if resp == 'signed'}
            declined = {visitor for _offer, (visitor, resp) in latest.items() if resp == 'declined'}
            withdrawn = {visitor for _offer, (visitor, resp) in latest.items() if resp == 'withdrawn'}
            conversions = {r['visitor'] for r in db.execute(
                'SELECT DISTINCT visitor FROM demand_conversion WHERE experiment=? AND day=?', (name, day))}
        active_signed_unconverted = active_signed - conversions
        proven_lower = len(conversions) + len(active_signed_unconverted)
        untested = exposed - offered
        upper = proven_lower + len(untested)
        obligations = []
        if name != BASELINE:
            for r in db.execute('SELECT o.event_id,o.visitor,o.valid_from,o.valid_to,o.auto_convert FROM offer o '
                                "WHERE o.experiment=? AND o.day=? AND o.kind='waitlist'", (name, day)):
                latest_resp = db.execute(
                    'SELECT response FROM offer_response WHERE offer_id=? ORDER BY at DESC,event_id DESC LIMIT 1',
                    (r['event_id'],)).fetchone()
                converted = db.execute('SELECT 1 FROM demand_conversion WHERE offer_id=?', (r['event_id'],)).fetchone()
                if not converted and latest_resp and latest_resp['response'] == 'signed' and (as_of is None or r['valid_to'] >= as_of):
                    obligations.append(dict(r))
        prices = sorted({r['price'] for r in db.execute(
            'SELECT DISTINCT price FROM offer WHERE experiment=? AND day=?', (name, day))})
        opportunity_denominator = len(offered)
        if opportunity_denominator < MIN_SAMPLE or len(prices) < 2:
            conclusion = '无法判定：样本不足以支持扩容或调价结论'
        else:
            conclusion = '可进入下一步评估'
        metrics = {
            'day': day, 'experiment': name, 'as_of': as_of,
            'plan': dict(plan_row) if plan_row else None,
            'page_views': total_views, 'sold_out_views': sold_views,
            'distinct_sold_out_visitors': len(exposed),
            'opportunity_denominator': opportunity_denominator,
            'proven_demand_lower_bound': proven_lower,
            'untested_visitors': len(untested),
            'unidentified_upper_bound': upper,
            'fulfilled_orders': len(conversions),
            'new_checkins_today': len(conversions),
            'waitlist_obligations': obligations,
            'waitlist_obligation_count': len(obligations),
            'price_rejection_sample': sorted(declined),
            'withdrawn_sample': sorted(withdrawn),
            'no_opportunity_views_excluded_from_rejection': sorted(untested),
            'prices_observed': prices,
            'conclusion': conclusion,
            'source_files': source_files,
        }
        db.execute('INSERT INTO demand_study_decision(day,experiment,metrics) VALUES(?,?,?) '
                   'ON CONFLICT(day,experiment) DO UPDATE SET metrics=excluded.metrics',
                   (day, name, json.dumps(metrics, ensure_ascii=False, sort_keys=True)))
        decisions.append(metrics)
    return decisions


def simulate_world(latent, plan, offers):
    """反例仿真器：给定潜在愿买世界与邀约集合，确定性生成响应包。

    latent 是标注为仿真输入的潜在意愿表，不是真实采集证据，不进入正式报表；
    输出包可像真实响应包一样被 offer-import 重放，用于演示两个潜在需求世界
    共享同一 booking/funnel 行记录却产生不同的已证愿买人数。即时方案在意愿
    为真时附带成单；保留房间的候补方案只产生预授权响应，当天不兑现。
    """
    willing = latent['willing']
    responses, conversions = [], []
    for offer in offers:
        visitor = offer['visitor']
        if willing.get(visitor):
            responses.append({'event_id': f"sim-{offer['event_id']}-sign", 'offer_id': offer['event_id'],
                              'visitor': visitor, 'at': offer['valid_from'], 'response': 'signed',
                              'price': offer['price']})
            if plan['plan_kind'] == 'instant':
                conversions.append({'event_id': f"sim-{offer['event_id']}-conv", 'offer_id': offer['event_id'],
                                    'vacancy_id': offer['vacancy_id'], 'visitor': visitor,
                                    'day': offer['day'], 'price': offer['price'],
                                    'at': offer['valid_from']})
        else:
            responses.append({'event_id': f"sim-{offer['event_id']}-decline", 'offer_id': offer['event_id'],
                              'visitor': visitor, 'at': offer['valid_from'], 'response': 'declined',
                              'price': offer['price']})
    return {'simulated_from_world': latent['world'], 'note': '仿真响应包，非门店真实签署',
            'experiment': plan['experiment'], 'responses': responses, 'conversions': conversions}
