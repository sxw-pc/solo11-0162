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
python -m petboard claim-import --db state.sqlite --file fixtures/claim_direct.json
python -m petboard insurer-import --db state.sqlite --file fixtures/insurer_direct.json
```

可分别导入 `intake_bad.json`、`care_bad.json`、`handoff_bad.json`；原始文件 SHA 写入 `file_journal`，事件 ID 异内容重放被拒绝。业务状态由 `intake_decision`、`care_decision`、`handoff_decision`、`medical_claim_decision` 和不可覆盖的 `decision_history`/`claim_decision_history` 查看。

接收检查芯片、兽医签署、疫苗覆盖、申请药物禁忌及后续证书撤销；护理检查计划批准、食谱和药物禁忌、独立给药见证、漏餐补偿与更正事件；交还检查领回授权、独立见证、伤病差异、独立责任复核、逾期费用和争议冻结。`fee_line` 保留结算项目及所依据的合同，伤病争议在 `injury_case` 中留状态。三个子系统以动物身份与有效版本联结，但每个都能独立采录、产生自己的待核状态。

## 医疗费用清分

`claim-import` 采录医院账单行（芯片号、就诊时点、金额）、保单责任期与免赔额；`insurer-import` 采录保险审核回执与银行流水（成功/退票/冲正）。`medical_claim_decision` 按 `claim_id` 给出 `covered_medical`、`deductible`、`insurance_payable`、`insurance_paid`、`owner_out_of_pocket`、`hospital_outstanding`、`insurance_outstanding`、`owner_pending_reimbursement`、`recovery_due` 和 `settlement_route`。三条独立事实分开记账：账单行是已发生的医疗费，审核回执只是授权，只有银行 `settled` 流水才移动资金。两条合同路线均被支持：

```powershell
# 直付路线：保险付医院 1000，主人付医院 200 免赔额，医院收齐 1200
python -m petboard claim-import --db state.sqlite --file fixtures/claim_direct.json
python -m petboard insurer-import --db state.sqlite --file fixtures/insurer_direct.json
# 报销路线：主人先付清 1200，保险后向主人实付 1000；入账前状态为 approved_unpaid
python -m petboard claim-import --db state.sqlite --file fixtures/claim_reimburse.json
python -m petboard insurer-import --db state.sqlite --file fixtures/insurer_reimburse_approval.json
python -m petboard insurer-import --db state.sqlite --file fixtures/insurer_reimburse_bounced.json
python -m petboard insurer-import --db state.sqlite --file fixtures/insurer_reimburse_settled.json
# 迟到既往伤复核：待付理赔撤回，已成功转账转为 recovery_item 追回债权
python -m petboard handoff-import --db state.sqlite --file fixtures/late_preexisting.json
```

同一 `transaction_id` 不可同时记作医院直付与主人报销；退票/冲正按 `reverses` 回退原入账。`handoff.py` 冻结的寄养服务费在 `fee_line`/`handoff_decision` 中，永不混作医疗发票。

本项目只使用合成数据与离线签署标签。兽医签署、身份核验、保险审核和银行付款均为本地证据模拟，不连接真实机构。
