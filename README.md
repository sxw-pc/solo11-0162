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

本项目只使用合成数据与离线签署标签。兽医签署、身份核验和付款均为本地证据模拟，不连接真实机构。

## 动物训练试次采录与主人可用范围

训练链的主营结果是**主人能在何种条件下安全执行哪种召回**，不是寄养护理费或笼位资格。旧的简单场次导入（`trials`：successes/attempts + 视频路径）保留可用，但只作为粗粒度训练观察，永远不能单独驱动毕业。

新链路按 `training-import` 的四类证据重算：

1. `clips`：本地视频片段索引（路径、sha256、帧数、时长、附属 RFID/位置日志、可复核时间窗清单）。
2. `recall_trials`：逐次召回，含起止时间窗、执行主人、口令/手势（`none/voice/gesture/voice+gesture`）、场景、干扰、镜头外提示、身体引导、重复提示、实际到达、压力/回避动作、标注人与待核状态、连拍 `burst_id`/`duplicate_of`。
3. `protocols`：**测试前**登记并封存的盲测协议（随机化口令/位置、被测主人、允许提示、独立观察者、最少试次、通过阈值、福利停止条件）。
4. `reviews` 与 `welfare_events`：测试后按原片核查的镜头外提示/重复试次与签署偏差，以及追车、持续压力等福利停止事件。

试次核查规则（`petboard/training.py::adjudicate`）：连拍帧只计一次独立试次；镜头外提示、身体引导、教练反复提示后的到达不计入无提示召回；标注争议保持 `pending` 且不进入任何成功率；福利停止后开始的试次一律排除。能力按「场景 × 提示层级 × 处理者」分层，处理者与室内/封闭室外/开放路边之间不默认迁移。

只有**封存协议 + 真实独立试次 + 原片复核 + 现场福利停止证据**全部闭合，`scope_credential` 才会发布一个主人可用范围（如 `cued-recall:indoor`、`independent-recall:enclosed-pen`）。出现追车或持续压力的场景写入 `blocked` 凭证，任何成功率都不能覆盖它。`graduation_decision.credential` 是已发布范围的列表，而不是一个笼统的 “recall”。

```powershell
python tools/build_training_fixtures.py
python -m petboard training-import --db state.sqlite --file fixtures/training_evidence_clips.json
python -m petboard training-import --db state.sqlite --file fixtures/training_evidence_sessions.json
python -m petboard training-import --db state.sqlite --file fixtures/training_evidence_route_a.json
python -m petboard training-import --db state.sqlite --file fixtures/training_evidence_route_b.json
python -m petboard training-verify --db state.sqlite --clip-root fixtures/training_video
python -m petboard report --db state.sqlite
python -m petboard audit --db state.sqlite
python -m unittest discover -s tests -v
```

完整验收叙述（每个试次采用/排除原因、A/B 两条路线、为何全量平均或按场景分组成功率仍会误发证）见 [`docs/training_chain.md`](docs/training_chain.md)。
