# 受限 YAML 配置目录服务与配置消费分析仓

Python 3.11 标准库实现（无第三方依赖），严格按题面合同控制类型、锚点/别名与
merge 语义，并把“有效配置 + 每键来源证据 + 关系表血缘”持久化到真实 SQLite。
在此之上还提供**外观抽检联合证书**：检测台缺测时按有序候补引用他台原测格或
配置标准样，采购单要求多指标且两两结论依据独立才予签发。

## 目录结构

```
confcat/
  errors.py         # 合同级错误类型（解析期/消费期分离，整次任务可回滚）
  model.py          # 原始共享图 RawNode 与物化有效树 MatNode、来源证据模型
  yaml_parser.py    # 受限 YAML 解析器（词法 + 递归下降，游标不变量统一）
  materialize.py    # 别名解引用/循环检测 + merge 合同物化 + 来源证据生成
  codec.py          # 物化树 JSON 编解码（保留 int/str 双键区分）与取值辅助
  catalog.py        # 配置产品目录：版本化发布、路径查询、哈希校验（SQLite）
  csv_pipeline.py   # CSV 读取与按配置归一化（列映射/缺失值/分区/列血缘）
  ingest.py         # 消费仓：类型化取参、整次拒绝、任务结果持久化（SQLite）
  fallback.py       # 抽检候补关系图 + 解析引擎（本机实测/他台原测格/标准样）
  certificate.py    # 联合证书：采购单评估、依据独立匹配、签发与送审持久化
  cli.py            # 人工检查命令行
samples/            # 多文档同名锚点、merge 序列冲突、显式键前后等 YAML 样例
                    # 以及外观抽检配置/采购单/循环配置样例
data/               # 合成 CSV 与运行期 SQLite
demo/run_demo.py             # 原题端到端演示（含断言）
demo/run_inspection_demo.py  # 外观抽检演示：两次送审不同结果（含断言）
tests/              # unittest：真值表、逐行关系表、隔离、重开、规模、深度、抽检
```

至少两个实质模块的分工：`yaml_parser`/`materialize`/`codec` 负责 YAML 目录与
来源，`csv_pipeline`/`ingest` 负责 CSV 数据转换与消费；抽检功能中
`fallback` 负责候补关系与解析，`certificate` 负责评估签发与持久化。

## 合同实现要点

### 类型与标量
- 仅双/单引号字符串、十进制整数（`0` 或负号可选的非零首位，正则 `^(0|-?[1-9][0-9]*)$`）、
  `true`/`false`/`null`；其余未加引号标量（`yes`、`001`、日期、`1.5`、`-0`）一律字符串。
- 映射键仅允许 `str`/`int`；`1` 与 `"1"` 是两个键，编解码显式带 `kt` 类型标签持久化。
- 显式拒绝：自定义 tag（`!`）、flow 集合（`[`/`{`）、块标量（`|`/`>`）、
  Tab 缩进、重复显式键、bool/null 键。

### 锚点与别名
- 锚点作用域为**当前文档**：多文档同名锚点互不影响；前向/未知别名、重名锚点拒绝。
- 自引用可通过解析期（pending 锚点），循环在物化期经 active 集合确定性抛
  `CircularReferenceError`，别名链逐跳记录（别名 -> 锚点位置）。
- 原始图保留共享子结构（别名指向同一锚点节点）；物化时每个插入点生成**独立子树**，
  从根上保证消费方修改不可能经别名影响其他任务。

### merge 键 `<<`
- 裸 `<<` 走内部哨兵识别；引号 `"<<"` 是普通字符串键，不触发 merge。
- merge 值为映射或映射序列，否则 `MergeTypeError`。
- 序列从左到右：第一个提供的键胜出，后者只补缺；当前映射**显式键恒覆盖** merge，
  与 `<<` 文本位置无关（前/后均有测试）。
- 只复制“键 -> 值引用”，**不递归合并**同名子映射（整体占位）。
- 每个有效键的 `Provenance` 记录：值/键位置、是否显式、别名链、每个 merge 候选的
  `source_index/won/overridden_by` 覆盖证据。

### 目录与消费仓
- 目录可发布多个命名产品，每次发布产生不可变新版本；任务绑定产品+版本快照，
  其他产品或新版本发布不影响在途/已完成任务。
- 任务按固定路径类型化取得 `source_uri`(str)、`column_mapping`(str->str)、
  `missing_value`(可选)、`partition_by`(可选)。
- 路径缺失、错误类型、原 CSV 列缺失、列映射冲突、分区键缺失 -> **整次任务拒绝**，
  单事务回滚，任务与结果均不落库。
- 归一化表逐行写入 SQLite，每列保留目标列、原 CSV 列、表头行号与配置来源 Provenance。
- 重开目录/任务仓可回查全部结果与配置链；发布快照含源文本 SHA-256，`verify` 校验。

### 外观抽检联合证书（fallback / certificate）
- **候补关系**：`stations.<台>.columns.<指标>` 声明本机实测列；`fallback.<指标>`
  给出缺测时的有序候补——`cell: {station, metric}` 引用他台原测格（递归解析），
  或 `standard:` 使用标准样声明。目录级 `describe()` 不需要测量即可解释每格
  的候补序、配置行号与别名链。
- **依据身份**：实测依据 = `meas:<接入任务>:<台>:<指标>`（他台引用本格即共用）；
  标准样依据 = 声明锚点身份（不同路径用别名引用同一声明 => 同一份依据；
  两处内联同值声明是两份）。解析输出本格实际来源与完整引用链，链中台/指标
  以类型化键落库；未走到的候补只登记 `skipped`，不占用依据。
- **循环/全缺可查**：循环候补按支路失败并记录环路径，全部分支失败即无有效
  结论；`resolve_all()/unresolved()` 与送审报告的 `unresolved_cells` 可检索。
- **联合证书**：采购单逐项声明指标/可接纳台/合格闭区间；每项选一个范围内
  结论，且任意两项最终依据不同（指标->依据二部匹配，Kuhn 增广）。不能签时
  报告给出：自身无合格结论的指标（逐台原因），或互相争用的极小指标组及其
  争用依据清单（删除法求极小不可满足子集）。
- **送审与重开**：送审记录与证书自包含落 SQLite（结论、依据台账、引用链、
  配置版本、订单快照、SHA-256 摘要），源 CSV 离线后仍可重开校验；后到的
  独立测量换新 CSV 重新送审，同一 order 历史可回查。CSV 接入完全复用
  `ingest` 段与 `TaskRepository`，原导入合同不变。

## 设计取舍：物化独立树 vs 保留共享图按需解引用

本实现**两者都保留**：解析期保留共享锚点图（表达共享事实、支撑同一性检查与别名链），
发布时物化为带完整来源证据的独立树并落库。代价是大范围别名复用时物化有额外内存/时间
（已用 100 配置 × 万节点、200 路别名复用测试覆盖）；收益是消费隔离由数据结构天然保证、
来源证据随树自包含、重开无需重放解析。

## 运行

```bash
# 端到端演示（发布/检查/任务/拒绝/修订有效性/隔离/重开，含内置断言）
python3 demo/run_demo.py

# 外观抽检演示：两次送审不同结果（拒签 -> 独立测量重审签发）+ 循环/全缺 + 离线重开
python3 demo/run_inspection_demo.py

# 全量测试（秒级）
python3 -m unittest discover -s tests -v

# 题面满规模基准（100 配置 / 万节点 / 10 万 CSV 行）
CONFCAT_FULL=1 python3 -m unittest tests.test_scale -v

# 人工检查 CLI
python -m confcat.cli publish data/catalog.db orders-product samples/orders_config.yaml
python -m confcat.cli products data/catalog.db
python -m confcat.cli query data/catalog.db orders-product doc-0 ingest/tier
python -m confcat.cli verify data/catalog.db orders-product

# 抽检 CLI：解释候补关系 / 送审 / 送审历史 / 离线重开证书
python -m confcat.cli publish data/catalog.db appearance samples/appearance_config.yaml --docs inspect
python -m confcat.cli fallbacks data/catalog.db appearance inspect
python -m confcat.cli submit data/catalog.db data/tasks.db data/certs.db \
    sub-1 appearance inspect samples/order_po001.yaml data/stations_v1.csv
python -m confcat.cli submissions data/certs.db --order PO-2026-001
python -m confcat.cli certificate data/certs.db cert-sub-1
```

无第三方依赖，仅用 Python 3.11 标准库；SQLite 数据库文件为真实持久化产物。
