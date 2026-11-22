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
python -m petboard kennel-import --db state.sqlite --file fixtures/kennel_ok.json
```

可分别导入 `intake_bad.json`、`care_bad.json`、`handoff_bad.json`；原始文件 SHA 写入 `file_journal`，事件 ID 异内容重放被拒绝。业务状态由 `intake_decision`、`care_decision`、`handoff_decision` 和不可覆盖的 `decision_history` 查看。

接收检查芯片、兽医签署、疫苗覆盖、申请药物禁忌及后续证书撤销；护理检查计划批准、食谱和药物禁忌、独立给药见证、漏餐补偿与更正事件；交还检查领回授权、独立见证、伤病差异、独立责任复核、逾期费用和争议冻结。`fee_line` 保留结算项目及所依据的合同，伤病争议在 `injury_case` 中留状态。三个子系统以动物身份与有效版本联结，但每个都能独立采录、产生自己的待核状态。

本项目只使用合成数据与离线签署标签。兽医签署、身份核验和付款均为本地证据模拟，不连接真实机构。

隔离增强副本还提供笼位采录：`kennel-import` 接收笼位和占用时间，`kennel_decision` 当前只按接收资格与单笼容量作基础判定。晚到的兽医证据会重算该动物的笼位判定；目前没有跨动物接触追踪或异常隔离流程。

## 整店寄养方案（booking）

`booking-import` 采录预约组、必须同笼/必须隔离约束、时段换笼交接人和主人拆组授权：

```powershell
python -m petboard intake-import --db weekend.sqlite --file fixtures/booking_intake.json
python -m petboard kennel-import --db weekend.sqlite --file fixtures/booking_kennel.json
python -m petboard booking-import --db weekend.sqlite --file fixtures/booking_ok.json
python -m petboard report --db weekend.sqlite
python -m petboard audit --db weekend.sqlite
python -m petboard intake-import --db weekend.sqlite --file fixtures/booking_revoked.json
```

求解规则：已入笼动物的占用记录不可被新预约挤走；未通过接收资格的动物撤出可执行候选，同组其余动物按主人书面拆组授权决定能否单独入住；容量按每个时间段同时占用核对；换笼要求起止两侧都有容量且有交接人，同一动物不会同时出现在两个笼位。结果保留 Pareto 未受支配方案（按期入住动物数、保持同组完整数两项指标，不设全店统一偏好），每个预约组标记完整入住（full）/按授权拆组（split）/延后（delayed）/不可安排（unschedulable），并列出每只动物的笼位、时间段和未满足的约束。接收资格变化（如证书撤销）会通过 health → kennel → booking 联动重算方案，audit 可从源文件复建。
