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
python -m petboard arbitration-import --db state.sqlite --file fixtures/arbitration/05_arbitration_owner_ack_m2.json
python -m petboard handoff-import --db state.sqlite --file fixtures/handoff_ok.json
```

可分别导入 `intake_bad.json`、`care_bad.json`、`handoff_bad.json`；原始文件 SHA 写入 `file_journal`，事件 ID 异内容重放被拒绝且整批原子回滚。业务状态由 `intake_decision`、`care_decision`、`handoff_decision` 和不可覆盖的 `decision_history` 查看。

## 护理更正的证据谱系裁决

护理更正（`care_action.supersedes`）按同一动物、同一计划、同一动作种类引用父证据；更正文件允许先于被引文件到达，父证据未到齐前链条为 `pending_parent`，不计入已完成餐次并冻结相关服务费。裁决引擎按每条原动作构建有向更正链，并识别：

- `cyclic`：更正链成环；`cross_plan`：更正跨越计划或动作种类边界（父证据在场时导入即拒，迟到父证据到达后标记冻结）；
- `fork_pending`：两个互斥的活跃末端均有独立见证，仲裁证据到达前不得按 event_id 字典序或导入先后自动选边，两个末端不会算成两餐；
- `adopted_tip` / `invalidated_root`：独立仲裁证据认定一个有效末端，或由主人认定原动作无效；
- `conflicted_arbitration` / `invalid_arbitration`：互相冲突的仲裁并存，或仲裁不满足独立性要求，费用继续冻结。

仲裁证据经 `arbitration-import` 导入，支持主人签认（`owner_ack`，须由动物主人签署）、两名独立护理见证一致的 `panel_ruling`（见证人与签署人、末端动作的 actor/witness 均不同）和主人 `invalid` 认定。仲裁只能由原签署人撤销（`arbitration_revocations`）；更正可由 `care-import` 的 `correction_revocations` 撤回，撤回晚到时链路自动重算。每一版裁决追加写入只增的 `care_chain_ledger`，保留采用证据（`adopted_tip_id`、`arbitration_id`）、每个节点的 `active` 标志与 `excluded_reason`。

主人签认按批准食谱补餐的末端 → 该餐完成、不减服务费；两名独立护理见证确认未按批准食谱的末端 → 该餐未履约，`care_credit` 按服务费补偿，`payable_fee` 重算。主人事后撤销签认时，已结算 handoff 恢复冻结或补偿；重写前的 fee_line 快照保存在只增的 `fee_line_history` 中，旧决定可追溯。`report` 输出 `care_chains`（当前裁决）、`care_chain_history`（逐版谱系）、`arbitrations`、`correction_revocations` 与 `fee_line_history`；`audit` 用 `file_journal` 全量重放并比对终端裁决（版本号与到达次序型历史不参与比对）。

`fixtures/arbitration/` 是三层链 + 并行末端 + 父动作迟到 + 主人撤销的完整夹具（00–07 按序号导入），不同导入顺序得到相同终端裁决，由 `ArbitrationLineageCase` 覆盖。

接收检查芯片、兽医签署、疫苗覆盖、申请药物禁忌及后续证书撤销；护理检查计划批准、食谱和药物禁忌、独立给药见证、漏餐补偿与更正事件；交还检查领回授权、独立见证、伤病差异、独立责任复核、逾期费用和争议冻结。`fee_line` 保留结算项目及所依据的合同，伤病争议在 `injury_case` 中留状态。三个子系统以动物身份与有效版本联结，但每个都能独立采录、产生自己的待核状态。原有的单链更正行为与 intake/care/handoff 命令保持兼容。

本项目只使用合成数据与离线签署标签。兽医签署、身份核验和付款均为本地证据模拟，不连接真实机构。
