# 免费改签的候补兑现与放号规则试算

馆方票务的候补报价、预约和入园历史,以及下期放号政策登记。 `data/` 中的记录均为虚构业务数据。

使用 Python 3.11.2。资料核验与回放引擎仅依赖标准库,建议以隔离模式运行:

```bash
python3 -I -m unittest discover -s tests -v
```

## 数据资料核验

在仓库根目录核验资料格式、键值与关联记录:

```bash
python3 -I src/cli.py validate --data data --out outputs
```

结构性问题记为 error;业务异常(如无法对账的验票回单、释放条件不成立)记为
warn,写入 `outputs/findings.csv`,不做静默修正。

## 引擎命令

```bash
# 历史回放:复刻 legacy 已执行规则,输出报价结局、时点可用席位、交换链、验票对账
python3 -I src/cli.py replay --data data --out outputs

# 候选规则反事实对比(legacy / A / B),只在有随机概率记录的支撑集合内给界限
python3 -I src/cli.py compare --data data --out outputs

# 下一轮核准登记:未到期旧报价保护、场次容量、政策可上线/试行判定
python3 -I src/cli.py register --data data --out outputs

# 指定日期审计:哪些座位由改签腾出、后来由谁获得、是否验票入园
python3 -I src/cli.py audit --data data --out outputs --day 2026-11-02

# 解释某政策为何不能用于特定场次
python3 -I src/cli.py audit --data data --out outputs \
  --day 2026-11-02 --policy A --slot W00B

# 一次生成全部产出
python3 -I src/cli.py run-all --data data --out outputs
```

## 识别口径(摘要)

- 报价自 sent 到 expires 占席;任何时点的可用数只由该时点之前的事件计算,
  不用后来实际释放的座位回填,也不把确认后才释放的原票当作先发报价的依据。
- 未送达(not_delivered)与收到后拒绝(declined)严格分开;未观察到接受意愿
  的候补只给 [0,1] 界限,不假设新增候补必然接受。
- 反事实只覆盖 `randomization.csv` 中有概率记录的候选;交换链名额单独标注,
  单组效果不外推。界限过宽或无随机证据的规则留作试行。
- 家庭多人订单只有 `may_split=yes` 才允许部分改签。
- 新生效日仍未到期的既有报价按发出时约定继续有效;已用于换票的原票不可重复释放。

完整方法说明见 [METHODOLOGY.md](METHODOLOGY.md),各表字段约定见
[资料约定](data-contract.md)。

## 代码结构

```
src/waitlist/
  models.py          数据模型与常量
  loader.py          CSV 加载与跨表核验
  occupancy.py       时点正确的占用账本与报价生命周期
  policy.py          资格、排序、保留比例、原因码
  replay.py          历史规则回放与交换链台账
  counterfactual.py  反事实评估与界限
  metrics.py         历史与政策级指标汇总
  registry.py        下一轮核准登记与旧报价保护
  audit.py           日期审计与规则适用性解释
  report.py          CSV / Markdown 输出
tests/               资料格式测试 + 引擎因果约束测试(20 项)
```
