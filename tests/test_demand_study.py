import json
import os
import tempfile
import unittest
from pathlib import Path

from petboard.__main__ import run_import, audit
from petboard import demand_study
from petboard.report import report
from petboard.store import connect

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / 'fixtures' / 'stockout-experiment'
PAGES = EXP / 'pages'


def load(rel):
    return json.loads((EXP / rel).read_text(encoding='utf-8'))


def feed(db, kind, value, page_root=None):
    return run_import(db, kind, json.dumps(value, ensure_ascii=False).encode(), page_root=page_root)


def feed_file(db, kind, rel, page_root=None):
    return feed(db, kind, load(rel), page_root)


class StockoutExperimentBase(unittest.TestCase):
    def setUp(self, reuse_tmp=None):
        self.tmp = reuse_tmp or tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')
        feed_file(self.db, 'booking-import', 'common/01_booking_day_start.json')
        feed_file(self.db, 'funnel-import', 'common/03_funnel_views.json')
        feed_file(self.db, 'booking-import', 'common/02_booking_cancel_v2.json')
        feed_file(self.db, 'offer-import', 'common/04_identities.json')
        feed_file(self.db, 'offer-import', 'common/05_snapshots.json', page_root=str(PAGES))
        feed_file(self.db, 'offer-import', 'common/06_exposures.json')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def metrics(self, experiment):
        rows = report(self.db)['demand_studies']
        return next(row['metrics'] for row in rows if row['experiment'] == experiment)

    def raw_dedup_aggregate(self):
        """仅在原 booking/funnel 风格表上的去重求和：两个潜在世界里结果完全相同。"""
        return {
            'distinct_sold_out_visitors': self.db.execute(
                'SELECT COUNT(DISTINCT visitor) FROM page_exposure WHERE observed=?', ('sold_out',)).fetchone()[0],
            'sold_out_views': self.db.execute(
                'SELECT COUNT(*) FROM page_exposure WHERE observed=?', ('sold_out',)).fetchone()[0],
            'confirmed_bookings': self.db.execute(
                "SELECT COUNT(*) FROM booking_intent WHERE status='confirmed'").fetchone()[0],
        }

    def _build_world(self, latent_file):
        """在当前 db 上建一套仿真候补实验，按潜在意愿世界确定性生成响应包。"""
        plan = {'plans': [{
            'event_id': 'sim-plan-waitlist', 'experiment': 'sim-waitlist', 'day': '2026-11-07',
            'plan_kind': 'waitlist', 'signed_at': '2026-11-07T09:00:00', 'budget': 500,
            'hold_room': True, 'auto_convert': True, 'valid_hours': 24}]}
        assignments = {'assignments': [{
            'event_id': f'sim-asg-{v}', 'experiment': 'sim-waitlist', 'visitor': v,
            'assigned_at': '2026-11-07T09:05:00', 'cohort': 'waitlist',
            'sampling_token': f'sim-token-{v}'} for v in ('v-3', 'v-4', 'v-5', 'v-6', 'v-7')]}
        feed(self.db, 'offer-import', plan)
        feed(self.db, 'offer-import', assignments)
        sim_offers = []
        for offer in load('plan-a/08_offers.json')['offers']:
            item = dict(offer)
            item['event_id'] = f"sim-{offer['event_id']}"
            item['experiment'] = 'sim-waitlist'
            sim_offers.append(item)
        feed(self.db, 'offer-import', {'offers': sim_offers})
        latent = load(latent_file)
        self.assertTrue(latent['simulation_only'])
        pack = demand_study.simulate_world(latent, plan['plans'][0], sim_offers)
        feed(self.db, 'offer-import', {'responses': pack['responses']})
        return latent, self.metrics('sim-waitlist')


class PlanAWaitlistTests(StockoutExperimentBase):
    def setUp(self):
        super().setUp()
        feed_file(self.db, 'offer-import', 'plan-a/07_plan_assignments.json')
        feed_file(self.db, 'offer-import', 'plan-a/08_offers.json')
        feed_file(self.db, 'offer-import', 'plan-a/09_responses.json')

    def test_three_signed_one_withdrawn_leaves_lower_bound_two_and_two_obligations(self):
        m = self.metrics('exp-waitlist-2026-11-07')
        self.assertEqual(m['new_checkins_today'], 0)
        self.assertEqual(m['fulfilled_orders'], 0)
        self.assertEqual(m['proven_demand_lower_bound'], 2)
        self.assertEqual(m['waitlist_obligation_count'], 2)
        self.assertEqual(m['untested_visitors'], 0)
        self.assertEqual(m['unidentified_upper_bound'], 2)
        self.assertEqual(m['opportunity_denominator'], 5)
        self.assertEqual(m['withdrawn_sample'], ['v-5'])
        self.assertEqual(m['price_rejection_sample'], ['v-6', 'v-7'])
        self.assertTrue(m['plan']['hold_room'] and m['plan']['auto_convert'])
        for obligation in m['waitlist_obligations']:
            self.assertEqual(obligation['valid_to'], '2026-11-08T16:00:00')
        self.assertIn('无法判定', m['conclusion'])

    def test_held_room_cannot_convert_even_with_signed_preauthorization(self):
        bad = {'conversions': [{'event_id': 'conv-a-illegal', 'offer_id': 'off-a-v3',
                                'vacancy_id': 'b-3-cancel', 'visitor': 'v-3', 'day': '2026-11-07',
                                'price': 180, 'at': '2026-11-07T19:00:00'}]}
        with self.assertRaisesRegex(ValueError, '保留给迟到应急入住'):
            feed(self.db, 'offer-import', bad)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM demand_conversion').fetchone()[0], 0)

    def test_refresh_by_same_visitor_does_not_increase_demand_lower_bound(self):
        # v-6 两次售罄刷新（pv-4/pv-6 与 ex-v6-1018/ex-v6-1025），独立访客仍只计一次
        self.assertEqual(self.metrics('none')['distinct_sold_out_visitors'], 5)
        self.assertEqual(self.metrics('none')['page_views'], 7)


class PlanBInstantTests(StockoutExperimentBase):
    def setUp(self):
        super().setUp()
        feed_file(self.db, 'offer-import', 'plan-b/07_plan_assignment.json')
        feed_file(self.db, 'offer-import', 'plan-b/08_offer.json')
        feed_file(self.db, 'offer-import', 'plan-b/09_response_conversion.json')

    def test_one_sampled_offer_one_conversion_other_four_untested(self):
        m = self.metrics('exp-instant-2026-11-07')
        self.assertEqual(m['new_checkins_today'], 1)
        self.assertEqual(m['fulfilled_orders'], 1)
        self.assertEqual(m['proven_demand_lower_bound'], 1)
        self.assertEqual(m['waitlist_obligation_count'], 0)
        self.assertEqual(m['untested_visitors'], 4)
        self.assertEqual(m['unidentified_upper_bound'], 5)
        self.assertEqual(m['opportunity_denominator'], 1)
        self.assertEqual(m['price_rejection_sample'], [])
        self.assertEqual(m['no_opportunity_views_excluded_from_rejection'],
                         ['v-3', 'v-5', 'v-6', 'v-7'])
        self.assertIn('无法判定', m['conclusion'])

    def test_double_redemption_of_same_vacancy_rejected(self):
        with self.assertRaisesRegex(ValueError, '同一临时空房只能兑现一笔订单'):
            feed_file(self.db, 'offer-import', 'bad/double_redemption_same_vacancy.json')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM demand_conversion').fetchone()[0], 1)

    def test_sold_out_views_are_not_price_rejection_samples(self):
        baseline = self.metrics('none')
        self.assertEqual(baseline['price_rejection_sample'], [])
        self.assertEqual(baseline['proven_demand_lower_bound'], 0)
        self.assertEqual(baseline['unidentified_upper_bound'], 5)


class TwoWorldCounterexampleTests(StockoutExperimentBase):
    """两个潜在需求世界共享同一 booking/funnel/曝光行记录，愿买人数却完全不同。"""

    def test_same_raw_records_different_verified_willing_buyers(self):
        high_latent, high = self._build_world('worlds/latent_high.json')
        raw_high = self.raw_dedup_aggregate()
        high_verified = high['proven_demand_lower_bound']
        self.assertEqual(high_verified, 5)

        low_dir = Path(self.tmp.name) / 'low-world'
        low_dir.mkdir()
        low_tmp = tempfile.TemporaryDirectory(dir=low_dir)
        low_db = connect(Path(low_tmp.name) / 'state.sqlite')
        runner = StockoutExperimentBase('setUp')
        runner.tmp = low_tmp
        runner.db = low_db
        try:
            StockoutExperimentBase.setUp(runner, reuse_tmp=low_tmp)
            low_latent, low = runner._build_world('worlds/latent_low.json')
            raw_low = runner.raw_dedup_aggregate()
        finally:
            low_db.close()
            low_tmp.cleanup()

        # 两个世界原始 booking/funnel/曝光行记录逐字段一致
        self.assertEqual(raw_high, raw_low)
        self.assertNotEqual(high_latent['willing'], low_latent['willing'])
        # 但真实可证愿买人数完全不同
        self.assertEqual(low['proven_demand_lower_bound'], 1)
        self.assertEqual(low['waitlist_obligation_count'], 1)
        self.assertEqual(low['fulfilled_orders'], 0)
        # 仅在原表上去重求和在两个世界完全相同：无从知道售罄损失
        self.assertEqual(raw_high['distinct_sold_out_visitors'], 5)
        self.assertEqual(raw_low['distinct_sold_out_visitors'], 5)
        self.assertEqual(low['unidentified_upper_bound'], 1)
        self.assertEqual(high['unidentified_upper_bound'], 5)

    def test_simulation_pack_is_marked_and_latent_file_is_not_evidence(self):
        latent = load('worlds/latent_high.json')
        with self.assertRaises(ValueError):
            # 潜在意愿表不含任何合法事件段，且标注 simulation_only，不能当证据导入
            feed(self.db, 'offer-import', latent)
        offers = load('plan-a/08_offers.json')['offers']
        plan = {'event_id': 'sim-plan-x', 'experiment': 'sim-x', 'day': '2026-11-07',
                'plan_kind': 'waitlist', 'signed_at': '2026-11-07T09:00:00', 'budget': 500,
                'hold_room': True, 'auto_convert': True, 'valid_hours': 24}
        pack = demand_study.simulate_world(latent, plan, offers[:1])
        self.assertIn('仿真', pack['note'])


class ExposureAndReplayTests(StockoutExperimentBase):
    def test_exposure_state_must_match_snapshot_at_that_moment(self):
        with self.assertRaisesRegex(ValueError, '错误曝光'):
            feed_file(self.db, 'offer-import', 'bad/exposure_state_mismatch.json')

    def test_soldout_page_with_booknow_button_rejected_by_html_parser(self):
        snapshot = load('common/05_snapshots.json')['snapshots'][1]
        bad_dir = EXP / 'bad'
        with self.assertRaisesRegex(ValueError, 'book-now'):
            demand_study.parse_snapshot_file(bad_dir / 'page_soldout_with_booknow.html')
        with self.assertRaisesRegex(ValueError, 'book-now'):
            feed(self.db, 'offer-import',
                 {'snapshots': [dict(snapshot, event_id='snap-bad-html',
                                     page_file='page_soldout_with_booknow.html')]},
                 page_root=str(bad_dir))

    def test_parser_accepts_all_valid_synthetic_pages(self):
        for name in ('snapshot_0930_available.html', 'snapshot_1000_soldout.html',
                     'snapshot_1530_available.html'):
            parsed = demand_study.parse_snapshot_file(PAGES / name)
            self.assertEqual(parsed['price'], 180)

    def test_two_real_plans_cannot_be_merged_on_same_day(self):
        feed_file(self.db, 'offer-import', 'plan-a/07_plan_assignments.json')
        with self.assertRaisesRegex(ValueError, '同一房晚只能执行一份真实采集方案'):
            feed_file(self.db, 'offer-import', 'plan-b/07_plan_assignment.json')

    def test_replay_is_idempotent_and_different_content_conflicts(self):
        feed_file(self.db, 'offer-import', 'plan-b/07_plan_assignment.json')
        feed_file(self.db, 'offer-import', 'plan-b/08_offer.json')
        feed_file(self.db, 'offer-import', 'plan-b/09_response_conversion.json')
        feed_file(self.db, 'offer-import', 'plan-b/09_response_conversion.json')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM offer_response').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM demand_conversion').fetchone()[0], 1)
        changed = load('plan-b/09_response_conversion.json')
        changed['responses'][0]['price'] = 200
        with self.assertRaisesRegex(ValueError, 'event_id conflict'):
            feed(self.db, 'offer-import', changed)

    def test_audit_replay_matches_and_carries_demand_studies(self):
        feed_file(self.db, 'offer-import', 'plan-b/07_plan_assignment.json')
        feed_file(self.db, 'offer-import', 'plan-b/08_offer.json')
        feed_file(self.db, 'offer-import', 'plan-b/09_response_conversion.json')
        result = audit(self.db)
        self.assertEqual(result['status'], 'matched')
        self.assertGreaterEqual(result['demand_studies'], 2)


class BookingFunnelTransitionTests(StockoutExperimentBase):
    def test_confirmed_booking_flips_available_to_sold_out_booked_one_to_two(self):
        fresh = connect(Path(self.tmp.name) / 'transition.sqlite')
        try:
            slot = {'slots': [{'event_id': 'slot-t', 'day': '2026-11-08', 'capacity': 2}]}
            first = {'intents': [{'event_id': 'bt-1', 'day': '2026-11-08', 'visitor': 'u-1',
                                  'price': 180, 'status': 'confirmed'}]}
            second = {'intents': [{'event_id': 'bt-2', 'day': '2026-11-08', 'visitor': 'u-2',
                                   'price': 180, 'status': 'confirmed'}]}
            views = {'views': [
                {'event_id': 'pt-1', 'day': '2026-11-08', 'visitor': 'u-3', 'observed': 'available'},
                {'event_id': 'pt-2', 'day': '2026-11-08', 'visitor': 'u-4', 'observed': 'sold_out'}]}
            feed(fresh, 'booking-import', slot)
            feed(fresh, 'booking-import', first)
            feed(fresh, 'funnel-import', views)
            before = report(fresh)
            self.assertEqual(before['booking_days'][0]['state'], 'available')
            self.assertEqual(before['demand_days'][0]['booked'], 1)
            feed(fresh, 'booking-import', second)
            after = report(fresh)
            self.assertEqual(after['booking_days'][0]['state'], 'sold_out')
            self.assertEqual(after['demand_days'][0]['booked'], 2)
            self.assertEqual(after['demand_days'][0]['sold_out_views'], 1)
            self.assertEqual(audit(fresh)['status'], 'matched')
        finally:
            fresh.close()

    def test_cancellation_does_not_rewrite_historical_soldout_exposure(self):
        # 取消后 booking_decision 回到 available，但 10:10 的旧曝光仍归属售罄快照
        booking = report(self.db)['booking_days'][0]
        self.assertEqual(booking['state'], 'available')
        old = self.db.execute("SELECT observed,opportunity FROM page_exposure WHERE event_id='ex-v3-1010'").fetchone()
        self.assertEqual((old['observed'], old['opportunity']), ('sold_out', 'none'))
        self.assertEqual(self.metrics('none')['no_opportunity_views_excluded_from_rejection'],
                         ['v-3', 'v-4', 'v-5', 'v-6', 'v-7'])

    def test_exposures_imported_before_cancellation_stay_attributed_to_soldout_snapshot(self):
        ordered = connect(Path(self.tmp.name) / 'ordered.sqlite')
        try:
            feed_file(ordered, 'booking-import', 'common/01_booking_day_start.json')
            feed_file(ordered, 'funnel-import', 'common/03_funnel_views.json')
            feed_file(ordered, 'offer-import', 'common/04_identities.json')
            feed_file(ordered, 'offer-import', 'common/05_snapshots.json', page_root=str(PAGES))
            feed_file(ordered, 'offer-import', 'common/06_exposures.json')
            # 取消发生在曝光入库之后
            feed_file(ordered, 'booking-import', 'common/02_booking_cancel_v2.json')
            row = ordered.execute("SELECT observed FROM page_exposure WHERE event_id='ex-v4-1012'").fetchone()
            self.assertEqual(row['observed'], 'sold_out')
            self.assertEqual(report(ordered)['booking_days'][0]['state'], 'available')
        finally:
            ordered.close()


if __name__ == '__main__':
    unittest.main()
