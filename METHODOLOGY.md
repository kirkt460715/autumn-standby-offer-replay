# 回放与反事实评估方法说明

本文件说明 `src/waitlist/` 引擎对候补改签规则的识别口径。所有结论只基于
`data/` 中带真实时刻的记录,不使用任何未来信息或未观察到的意愿假设。

## 1. 时点正确的席位账本

`occupancy.Ledger` 按事件时刻推进,任一时点 `t` 的占用只由 `t` 之前(含 `t`)
已发生的事件构成:

- 可用席位 = 容量 − 既有确认席位 + 已生效原票释放 − 在效报价占席。
- 报价自 `sent` 起按其 `seats` 占席,直到 `expires`;`declined`/`accepted`
  可提前结束占席,`expired`/`not_delivered` 在到期时刻结束。
- `pending` 是仍在占席的状态记录,不重复占席。
- 原票释放只在"家庭订单真实存在且位于被释放场次"时生效;空 origin 或订单场
  不符的释放只记录不生效。同一原订单只允许释放一次(`consumed_origins`)。
- `snapshot_at(t)` 独立重放,保证查询历史时点时不会被后续事件污染——
  例如 2026-11-02 的 P001 在 09:06 才能使用 P000 在 09:05 腾出的 W00A 席位,
  09:04 的可用数不会被该释放回填。

## 2. 历史回放口径

`replay.replay_history` 复刻 legacy 已执行规则,并对每个 `sent` 记录发出时点的
可用席位(`send_availability.csv`)。报价结局严格分区:

| 结局 | 含义 | 是否观察到接受意愿 |
|---|---|---|
| accepted | 确认改签 | 是(接受) |
| declined | 收到后拒绝 | 是(拒绝) |
| expired | 送达但有效期内未作答 | 否 |
| not_delivered | 未送达(seen=no) | 否,且不等于拒绝 |
| pending | 截至资料截止仍在占席 | 否 |

交换链(`requires_offer`)单独建账:依赖报价的席位来源必须指向上游确认后已
生效的原票释放;单组换票的效果不外推到链上的其他名额。

验票记录(`gate.csv`)先与确认报价对账,再与原始订单对账;两者皆非的回单记
`gate_unreconciled`,从所有履约口径剔除并在 `findings.csv` 披露,不做静默修正。

## 3. 反事实识别策略

### 3.1 支撑集合

每个历史放号决策点(每日 09:00 的普通场)以 `randomization.csv` 中该时点、
该场次 `eligible=yes` 的候选为唯一有支撑的送达集合。集合之外的候补:

- 标记 `UNSUPPORTED_NO_RANDOMIZATION`,不假设其会被送达或接受;
- 依赖交换链腾出席位的名额标记 `CHAIN_UNSUPPORTED`。

### 3.2 响应分类与界限(Manski 式)

接受意愿只在"已送达且有终局作答"的报价上被观察:

- `accepted` → 接受概率点估计 1;`declined` → 0;
- `expired`、`not_delivered`、旧规则从未收到报价 → 未观察,接受概率界于 [0,1];
- 未送达与拒绝在所有报表中分开,永不合并。

聚合界限:

- 下界 = 所有未观察意愿按"拒绝"处理;
- 上界 = 所有未观察意愿按"接受"处理;
- legacy(`logged_priority`)按抽签 `probability` 加权(每决策点概率和为 1);
- A(`protected_first`)/ B(`longest_wait`)为确定性排序,只向排序第一且
  资格通过的候选发一个名额(CSV 中 `chosen=yes`),其余候选不贡献送达口径。

界限宽度大于零或存在无支撑/链上候选时,规则只能给界限并留作试行,不假设
"所有新增候补都接受"。

### 3.3 政策资格

`policy.check_basic_eligibility` 在每个决策时点检查:

1. 目标场次在候补登记的 `alternatives` 内;
2. 可用席位足够;部分改签只有 `may_split=yes` 才允许,否则 `SPLIT_FORBIDDEN`;
3. 普通候补自 `joined_at` 起的等待时长不超过政策的
   `max_regular_wait_minutes`(保障票不受此限);
4. 保障票需求不得超过 `regular_floor` 之外的 headroom
   (`FLOOR_PROTECTED_HEADROOM`)。

## 4. 下一轮核准与承诺保护

`registry.build_registry` 针对新生效日(当前数据为 2026-12-05):

- 生效时点仍未到期的既有报价按**发出时的约定**继续有效(沿用其实际
  `expires`,如 PN1 的 legacy 16 分钟有效期),新政策不得改动;
- 生效时点的容量扣除既有订单与旧报价占席后再计算可用数;
- 该轮无 `randomization` 记录时,A/B 一律登记为试行,可上线的只有确定性
  条款(排序、有效期、等待上限、保留比例)与旧报价保护;
- 已用于换票的原票列入 `consumed_origins`,不可重复释放。

## 5. 原因码

| 代码 | 含义 |
|---|---|
| NOT_ALTERNATIVE | 候补未把该场登记为可接受选项 |
| WAIT_CAP | 普通候补等待超过政策上限 |
| NO_FREE_SEAT | 决策时点席位不足(含未到期占席) |
| SPLIT_FORBIDDEN | 未登记可拆意愿,不允许部分改签 |
| FLOOR_PROTECTED_HEADROOM | 普通保留比例已满,保障票不得再占 |
| UNSUPPORTED_NO_RANDOMIZATION | 无随机/概率记录,反事实无支撑 |
| CHAIN_UNSUPPORTED | 交换链名额,单组效果不可外推 |
| POLICY_NOT_EFFECTIVE | 政策在该时点尚未生效 |
| GRANDFATHERED_OFFER | 既有报价按旧约定继续有效 |
| SLICE_TOO_SMALL | 支撑范围不足,只能给界限 |
| UNOBSERVED_PREFERENCE | 意愿未观察,不得假设接受 |

## 6. 输出物

- `findings.csv`:资料核验的错误与业务异常;
- `historical_offers.csv` / `send_availability.csv` / `release_chain.csv`
  / `gate_reconciliation.csv`:历史回放明细;
- `counterfactual_decisions.csv`:逐决策点、逐候选的资格、响应状态与界限;
- `policy_comparison.csv`:政策级汇总(含送达机会与界限宽度);
- `next_round_*.csv`:下一轮旧报价保护、场次容量与政策审批;
- `audit_<day>.csv`:指定日期腾座→再分配→验票的完整链路;
- `summary.md`:面向出票主管的结论摘要。
