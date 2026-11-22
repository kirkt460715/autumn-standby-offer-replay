# 资料约定

CSV 采用 UTF-8 编码。日期使用年、月、日，数值以十进制表示。字段为空表示来源未提供该项或该项不适用，含义以表约定为准。

一个时段令牌按一人计。订单中 may_split 为 yes 时可接受拆分，否则整组确认。确认新场之后原场才释放，先记 `accepted` 后记 `origin_released`；若原订单不属于该馆场次，空 origin 不产生释放。报价从 sent 到 expires 占席，即使随后没有观察到已读；明确撤回、拒绝或确认可结束待定占用。未送达的记录不代表拒绝。
`randomization` 只表示该决策内对剩余候选的随机送达记录。event 表的 seen 用于区分送达与最终作答。候补政策从生效时刻起使用，承诺保护期间按旧报价，候补时长以候补首次登记为准。周末工作人员可先处理保障票，但普通候补的最长允许等待和保留比例同时受 policy 约束。

## 文件来源

- `slots.csv`。可改签到达时段与容量。 记录键为 `slot`。
- `orders.csv`。放号前既有订单。 记录键为 `order_id`。
- `waitlist.csv`。等待家庭及可接受选项。 记录键为 `request`。
- `events.csv`。定时通知、回应和原票释放。 记录键为 `event`。
- `randomization.csv`。分配机会的候选与抽取概率。 记录键为 `draw`、`request`。
- `gate.csv`。最终检票回单。 记录键为 `entry`。
- `policies.csv`。现行与备选放号口径。regular_floor 为保留给普通候补的当期席位比例。 记录键为 `policy`。
