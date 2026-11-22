# 召回训练链验收说明：从试次采录到主人可用范围

本说明对应 `chip-101` 的合成案例，所有证据都可用 `fixtures/training_video/` 下的本地片段重放，
`fixtures/training_evidence_*.json` 是导入数据，`tools/build_training_fixtures.py` 可确定性重建全部片段与数据。

## 1. 事件顺序与每个试次的采用/排除原因

### 1.1 静院教练场（2026-11-02，`quiet-yard`，处理者 coach-a）

原始场次为 9 次成功 / 10 次试次，其中 8 次带手势，另 1 次（rt-q9）为语音+手势。逐次核查后：

| 试次 | 判定 | 原因 |
| --- | --- | --- |
| rt-q1 … rt-q9 | 采用（cued） | 有口令/手势的独立召回，9 次到达，进入「有提示」层，**不得**进入无提示能力 |
| rt-q9-frame-2 … frame-10 | 排除 | 同一次召回的连拍帧（`duplicate_of=rt-q9`），十帧不是十个独立试次 |
| rt-q10 | 采用（independent） | 唯一无提示试次，未到达并出现舔鼻/回避；无提示层 0/1 |

### 1.2 开放路边（2026-11-03，`open-roadside`，处理者 owner-a，高干扰车流）

| 试次 | 时间 | 判定 | 原因 |
| --- | --- | --- | --- |
| rt-r1 … rt-r3 | 09:00–09:04 | 采用 | 无提示到达 3 次 |
| rt-r4 | 09:06 | 采用 | 无提示失败，出现追车/扑车；09:25:20 登记福利停止（`welfare-stop-1`） |
| rt-r5 … rt-r7 | 09:10–09:18 | 采用 | 停止前的无提示失败（夹尾、喘气、回避） |
| rt-r8 | 09:22 | **待核 pending** | 标注人对「转头是否算到达」有争议；保持待核，不进入成功率，也不能被高分场次掩盖 |
| rt-r9 | 09:27 | 排除 | 晚于 09:25:20 福利停止，第二次追车；停止后不得为凑样本继续 |
| rt-r10 | 09:47 | 排除 | 晚于 09:45:00 第二次福利停止（持续压力/僵住） |

可计分的无提示试次为 7 次、到达 3 次，主人在车流附近的真实无提示率为 **3/7 ≈ 0.4286**，
而不是原始登记表面的 3/10；待核 1 次、福利排除 2 次都在报告中保留原因。

### 1.3 路线 A：室内有提示盲测（2026-11-05，`indoor`，被测主人 owner-a）

协议 `proto-a-indoor-cued` 于 11-04 预先登记并封存：随机化、独立观察者 observer-b、
允许 `voice/gesture/voice+gesture`、最少 8 次、阈值 0.8、福利停止条件含追车/僵住/扑车。

| 试次 | 判定 | 原因 |
| --- | --- | --- |
| rt-a1 … rt-a8 | 采用 | 批准提示范围内的独立观察，7 次到达、1 次失败 |
| rt-a9 | 排除 | 原片核查发现镜头外教练手势（`off_camera_cue`），评审 `review-a-1` 签署偏差 |
| rt-a10 | 排除 | 身体引导（`physical_guidance`），不是召回，同样在评审偏差中签署 |

8 个采用试次、7 次到达，率 0.875 ≥ 0.8，协议闭合 → 发布 **`cued-recall:indoor`（室内有提示召回）**。

### 1.4 路线 B：封闭室外适应 + 主人无提示盲测（2026-11-06/07，`enclosed-pen`）

- 适应场（coach-a）：rt-p1…rt-p8 中 rt-p7 因教练连续三次重复提示被排除
  （`repeated-prompt-not-unaided-recall`），其余为教练处理者的训练观察，不迁移给主人。
- 盲测协议 `proto-b-pen-independent` 只允许 `none`：rt-b1…rt-b9 为 9 个无提示独立试次、8 次到达；
  rt-b10 使用重复手势提示，被核查排除并由 `review-b-1` 签署偏差。
- 9 次、8 到达，率 0.8889 ≥ 0.8，协议闭合 → 发布 **`independent-recall:enclosed-pen`（封闭室外独立召回）**。

两条路线都守福利界限，但主人可承诺的范围不同；A 的任何试次都不能充当 B 或开放区的无牵引许可。

## 2. 分场景能力与盲测有效性

`report` 输出的 `capability_by_setting` 按场景分别维护 independent/cued 两个层，
每层只统计核查后 `included` 的试次，并单列 `excluded`、`pending` 数量与逐条原因、处理者清单：

- quiet-yard：cued 9/9 = 1.0；independent 0/1 = 0.0；处理者仅 coach-a。
- open-roadside：independent 3/7 ≈ 0.4286；pending 1；welfare 排除 2；处理者仅 owner-a。
- indoor：协议内 8 个采用试次 7/8 = 0.875。
- enclosed-pen：主人盲测 9 个无提示试次 8/9 ≈ 0.8889。

盲测有效性（`evaluate_protocol`）不是成功率门槛，而是闭合条件清单：封存先于测试、随机化、
观察者独立且不兼任处理者、被测主人执行、场景不漂移、试次晚于登记、无镜头外提示/重复试次
（或在评审偏差中显式签署）、无待核争议、达到最少试次与阈值、场景未触发福利停止。
任一条件不满足，该协议 `valid=false` 并给出失败原因，不发布范围。

## 3. 主人可用范围（毕业决定）

最终 `graduation_decision.credential`：

```
cued-recall:indoor;independent-recall:enclosed-pen
```

`scope_credential` 同时保留两条 `blocked` 记录：`independent-recall:open-roadside` 与
`cued-recall:open-roadside`，理由是 2026-11-03T09:25:20 的追车福利停止；该场景在停止证据解除前
不得再安排盲测，任何历史成功率都不能覆盖它。系统没有任何「开放路边无牵引召回」凭证。

## 4. 为什么只换成全量平均或按场景分组成功率仍会误发证

用同一批 57 行原始标注做两种朴素算法：

1. **全量平均**：43 次到达 / 57 行 ≈ **0.7544**；即使剔除 9 行连拍重复帧，仍是 34/48 ≈ **0.7083**。
   - 它把教练有提示的 9 次静院成功和主人无提示失败混在一个分母里，处理者与提示层级被抹平；
   - 阈值若略降到 0.7 就会直接发「recall」证，而主人在车流旁的真实无提示率只有 0.4286 且发生过两次追车。
2. **按场景分组成功率**（剔除重复帧、但不做其余核查）：
   - quiet-yard 9/10 = 0.9、indoor 8/10 = 0.8、enclosed-pen 14/18 ≈ 0.7778、open-roadside 3/10 = 0.3。
   - 静院 0.9 这一组本身就是 9 次手势辅助 + 1 次无提示失败，分组率照样给该场景打「通过」，
     输出等价于「主人可在室外自由召回」的错误承诺；
   - 分组率无法识别 rt-a9 的镜头外提示、rt-a10 的身体引导、rt-b10/rt-p7 的重复提示，
     也无法表达 rt-r8 的待核状态与 rt-r9/rt-r10 的福利停止——这些试次在纯成功率里只能被计入或丢弃，
     而不能带着原因保留；
   - 它同样发现不了固定顺序泄题或教练细微动作：本案例的随机化与独立观察者是协议字段，
     不是视频统计量。

因此发布主人可用范围的依据只能是**封存协议、真实独立试次、原片复核与现场停止证据的闭合**；
分层成功率是能力画像，不是发证机关。

## 5. 复现命令

```powershell
python tools/build_training_fixtures.py
python -m petboard intake-import --db state.sqlite --file fixtures/intake_ok.json
python -m petboard training-import --db state.sqlite --file fixtures/training_evidence_clips.json
python -m petboard training-import --db state.sqlite --file fixtures/training_evidence_sessions.json
python -m petboard training-import --db state.sqlite --file fixtures/training_evidence_route_a.json
python -m petboard training-import --db state.sqlite --file fixtures/training_evidence_route_b.json
python -m petboard training-verify --db state.sqlite --clip-root fixtures/training_video
python -m petboard report --db state.sqlite
python -m petboard audit --db state.sqlite
python -B -m unittest discover -s tests -q
```

`training-verify` 对本地片段重新计算 sha256，片段被改动会报 `hash-mismatch`、缺失会报 `missing`；
`audit` 从 `file_journal` 重放全部导入并比对报告，任何库内手工篡改都会使重放不一致。
