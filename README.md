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

## 物业边界噪声采录

`noise-import` 收声级计离线包：设备身份、校准、地点、统一采样窗（10 分钟网格）、频谱摘要与原始文件摘要，以及录音片段索引（声纹分类）、道路背景观测和停场/屏障对照试验。`complaint-import` 收相邻物业投诉；`remediation-import` 收整改措施与现场复测。

`acoustic.py` 在边界点与庭院点的统一时窗上按能量单位计算等效连续声级（Leq），声压叠加与背景扣除一律用能量单位，不做 dB 算术相减。归责分两级证据：可比（风况与道路背景相近）的停场/屏障对照试验为决定性证据；否则用同步声纹、庭院—边界传播路径与道路背景做被动分离。两个同频来源同时混入时被动观测不能唯一确定店源剂量，时段判 `uncertain`，投诉保持 `pending` 并生成可执行的停场/屏障对照试验计划；不同气象条件下的静音片段不作为反证。无有效隔离试验时不能靠最高峰或事后挑选录音判 upheld。

投诉决定列出被采用的时窗、店源剂量、背景扣除依据与不确定部分。投诉成立后，整改验收必须落在已确认的店源路径上，措施必须是已安装/已执行（拟建围挡不算），现场复测店源剂量达标后，`business_scope` 才发布可承诺的室外活动时段（如围挡路线开放至 21:00，或停场路线 20:00 后关闭室外活动而寄养继续）；各路线证据独立，投诉发生时的责任保留在 `noise_history`。`fixtures/` 内含货车短峰、犬吠持续、相关两源、可比/不可比停场试验与两条整改路线的完整可重放案例，`report`/`audit` 可复核。
