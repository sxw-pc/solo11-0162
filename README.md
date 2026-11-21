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
