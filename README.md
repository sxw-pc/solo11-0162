# 宠物寄养交接采证台

面向虚构的寄养门店，离线采集动物接收资格、护理执行、授权交还与费用责任。三个业务子系统分别拥有事件、持久化决策和 CLI 入口。迟到兽医声明会使护理及交还结论重算，旧证据和旧决策保留在审计历史中。

## 运行

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m petboard demo --db demo.sqlite
python -m petboard report --db demo.sqlite
python -m petboard audit --db demo.sqlite
python -m unittest discover -s tests -v
```

独立入口：

```powershell
python -m petboard intake-import --db state.sqlite --file fixtures/intake_ok.json
python -m petboard care-import --db state.sqlite --file fixtures/care_ok.json
python -m petboard handoff-import --db state.sqlite --file fixtures/handoff_ok.json
```

可分别导入 `intake_bad.json`、`care_bad.json`、`handoff_bad.json`；原始文件 SHA 写入 `file_journal`，事件 ID 异内容重放被拒绝。业务状态由 `intake_decision`、`care_decision`、`handoff_decision` 和不可覆盖的 `decision_history` 查看。

接收检查芯片、兽医签署、疫苗覆盖、申请药物禁忌及后续证书撤销；护理检查计划批准、食谱和药物禁忌、独立给药见证、漏餐补偿与更正事件；交还检查领回授权、独立见证、伤病差异、独立责任复核、逾期费用和争议冻结。`fee_line` 保留结算项目及所依据的合同，伤病争议在 `injury_case` 中留状态。三个子系统以动物身份与有效版本联结，但每个都能独立采录、产生自己的待核状态。

## 售罄实验与未成交需求（offer-import / demand_study）

售罄页面的浏览次数推不出愿买人数：没有购买机会的浏览不是价格拒绝样本。`offer-import` 只收带同意、带时刻、可重放的真实证据——价签快照（含本地合成 HTML 页校验，售罄页禁止出现“立即下单”按钮）、经同意的访客身份、页面实际显示状态与机会、预先签署的实验方案与随机分配、定向邀约、固定价格预授权响应、撤回与实际成单。

关键边界：

- 同一访客多次刷新不增加需求下界，指标按独立访客去重；
- 同一临时空房（取消释放）最多兑现一笔订单，同房双兑现直接拒绝；
- 候补邀约必须写清有效时段与“空房出现时是否自动转单”；保留给迟到应急入住的房间当天不得转单，预授权不伪装成已入住；
- 历史曝光按当时页面快照归属，事后取消使房晚回到 available 也不倒写旧访客的机会；
- 同一房晚只允许一份真实采集方案，两份方案的响应包分开重放，禁止拼合成未执行的全量实验；
- 报表分开给出已证需求下界、尚未获得购买机会的访客数、不可识别上界与实际机会分母；样本不足时结论固定为“无法判定”，不给单点预测值；
- `fixtures/stockout-experiment/worlds/` 是标注 `simulation_only` 的潜在意愿仿真输入，不能作为证据入库，仅用于生成可重放的反例响应包：两个潜在需求世界共享完全相同的 booking/funnel/曝光行记录，可证愿买人数却分别为 5 和 1，证明仅在原表去重求和无从知道售罄损失。

复现（方案一：24 小时候补预授权，3 签 1 撤，新增入住 0、有效需求下界 2、候补责任 2）：

```powershell
python -m petboard booking-import --db plan-a.sqlite --file fixtures/stockout-experiment/common/01_booking_day_start.json
python -m petboard funnel-import  --db plan-a.sqlite --file fixtures/stockout-experiment/common/03_funnel_views.json
python -m petboard booking-import --db plan-a.sqlite --file fixtures/stockout-experiment/common/02_booking_cancel_v2.json
python -m petboard offer-import  --db plan-a.sqlite --file fixtures/stockout-experiment/common/05_snapshots.json --page-root fixtures/stockout-experiment/pages
python -m petboard offer-import  --db plan-a.sqlite --file fixtures/stockout-experiment/common/04_identities.json
python -m petboard offer-import  --db plan-a.sqlite --file fixtures/stockout-experiment/common/06_exposures.json
python -m petboard offer-import  --db plan-a.sqlite --file fixtures/stockout-experiment/plan-a/07_plan_assignments.json
python -m petboard offer-import  --db plan-a.sqlite --file fixtures/stockout-experiment/plan-a/08_offers.json
python -m petboard offer-import  --db plan-a.sqlite --file fixtures/stockout-experiment/plan-a/09_responses.json
python -m petboard report --db plan-a.sqlite
python -m petboard audit  --db plan-a.sqlite
```

把 `plan-a/` 换成 `plan-b/` 即复现方案二（随机抽 1 人发即时邀约并成单：新增入住 1、已证下界 1、候补责任 0，其余 4 人无购买机会，1/5 不得当作全体转化率）。`health.py` 与 `handoff.py` 的接收、交还与费用裁决不受实验影响；已完成预订、入住与撤回都保留真实发生事实。

本项目只使用合成数据与离线签署标签。兽医签署、身份核验和付款均为本地证据模拟，不连接真实机构。
