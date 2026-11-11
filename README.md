# 防护用品批次追溯采集

离线采集工程使用 Python 标准库和 SQLite。adapter 读取本项目的真实来源格式，sources 提供可重复响应序列，capture 原子归档解析后的来源记录。`--capture` 保留为原始观测入口，只记录原始观察、不作业务投影；`--run` 在同一工程内实现批次追溯工作流。

## 环境与准备

推荐 Python 3.12.12，支持 Python 3.11+；requirements.lock 为空表示没有第三方包，运行、测试和验收均不下载数据或依赖。Dockerfile 提供同一 Linux 环境。需要容器时先在正式运行之外准备：

```sh
docker build -t q005-procurement-evidence:initial .
docker run --rm q005-procurement-evidence:initial
```

## 基线测试与原始采集

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q collector_05 tests acceptance
scratch=$(mktemp -d)
python3 -m collector_05.cli --database "$scratch/raw.sqlite3" --capture fixtures --route /mail/one
python3 -m collector_05.cli --database "$scratch/raw.sqlite3" --capture fixtures --route /mail/one
```

第一次 decoded=1、inserted=1，重抓同一观测 inserted=0。临时库不提交。tests 是已实现采集基础能力的回归测试，不代表扩展功能完成。

## 来源协议

邮件是供应商合成批次包，X-Supplier 与 X-Batch 给出来源身份，附件 JSON 给 manufacturer、certificate、issued_at、receipt、use 等。仅按 aliases 归一批次号，supplier 不能省略；同批 manufacturer 冲突进入 quarantine，不合并凭证。相同文件名不同摘要都要归档，documents 是该批不同摘要的凭证数，不是邮件数。相同 Message-ID 内容变了也不得覆盖旧档。

附件 revoked_at 是证书失效事件，不是删除文件命令。按 as_of 输出当前证书有效性与受影响入库/使用链；原验收 accepted_at、result 永久保留，不因失效被重写为当时拒收。历史查询不得使用尚未发生的撤销；重复邮件不增加 archives 或重复影响项。归档文件命名不得只依赖原文件名。

FixtureTransport 每次进程创建从 routes.json 首条响应开始，同一路由多次 request 依次推进，最后一条可重复；calls 保存请求证据。源数据相对 fixture_root 读取，不能越出该目录。所有响应是本地合成数据，不发出公网请求。单场景 JSON 的 fixture_root 相对当前工作目录解析；在仓库根目录执行即可。run_id 是采集批次标识，name 仅作展示标签。

## 追溯工作流（--run）

```sh
python3 -m collector_05.cli --run fixtures/aliases.json --database collector_05.sqlite3 --output aliases.json
```

场景文件是一个 JSON 对象，字段如下；fixtures 内的 `aliases.json`、`collision.json`、`revoked.json`、`historical.json` 是可直接使用的样例，`fixtures/scenarios.json` 由验收脚本驱动：

| 字段 | 含义 |
| --- | --- |
| `name` | 展示标签，不参与业务判断 |
| `run_id` | 采集批次标识，与来源、Message-ID、摘要共同构成去重键 |
| `sources` | 有序根来源路由列表，按序经 FixtureTransport 拉取 |
| `options.aliases` | 来源批次号到规范批次号的映射；未列出的批次号原样保留 |
| `options.as_of` | 可选，证书有效性与失效影响的查询时点（`YYYY-MM-DD` 或 ISO-8601），缺省取当前 UTC |
| `fixture_root` | 本地合成来源根目录，routes.json 与 .eml 均在其下 |
| `portal_database` | 旧版字段，工作流忽略；状态全部落在 `--database` |

### 归批与冲突

- 批次身份是 `(X-Supplier, 规范批次号)`：supplier 缺失直接拒绝导入；批次号仅按 `aliases` 归一，未配置映射的原值保留；不同 supplier 即使批次号相同也不合并。
- 同一批次出现不同 `manufacturer` 时，冲突凭证进入 `quarantine`（`reason=manufacturer_conflict`），其证书、验收链不与原批次合并；原始凭证仍然归档，可在 quarantine 行的 `digest` 上反查。
- `batches[].documents` 是该批次已合并凭证的**不同摘要数**，不是邮件数；冲突凭证不计入。

### 不可变归档

- 每个附件先按 SHA-256 摘要落盘到 `<数据库名>.archives/<摘要前两位>/<摘要>`，同名规则下同名文件不同摘要各自独立，文件名不依赖原始附件名。
- 摘要同时作为 `trace_attachments` 主键并保存原始字节 BLOB；任何重复导入、补件或“同 Message-ID 但内容变化”的邮件都只新增（或跳过），绝不覆盖旧档。
- `archives` 输出全库不同附件摘要总数；重复邮件不增加该值。
- 补件只向既有证书补充 `issued_at` / `revoked_at` 等空缺时点，不会抹掉已经记录的事实。

### 验收时点与证书失效

- 附件中的 `accepted_at`、`result`、`receipt`、`use` 在导入时固化为验收决策；之后证书失效只影响当前有效性判断，历史决策永久保留、不会被改写成当时拒收。
- 附件中的 `revoked_at` 表示证书失效事件，不是删除指令：归档文件与决策行均保留。
- `as_of` 时点尚未发生的撤销不参与判断；失效批次下的入库/使用链出现在 `affected`，重复邮件不会产生重复影响项。
- `current_valid` 只在存在相关证书且至少一张在 `as_of` 有效（已签发、未失效）时为 true；没有任何证书记录时为 false。

### 业务输出

`--output` 文件与标准输出均给出同一投影：`batches`、`archives`、`quarantine`、`affected`、`decisions`、`current_valid`，以及 `notifications`（本次新建的人工核验通知数；同一冲突重复导入第二次为 0）。整个场景可在同一数据库上由独立进程重复执行，投影保持一致。

### 从入库反查凭证链

```sh
python3 -m collector_05.cli --database collector_05.sqlite3 --trace IN-1
```

输出该入库单的验收事实（`accepted_at`、`result`、`use`）、决策依据摘要、批次下全部归档凭证（摘要、原文件名、Message-ID）以及证书的签发/失效时点；归档原件可按摘要到 `.archives` 目录取回。

## 扩展接口与可观察输出

`python3 -m collector_05.cli --run 场景文件 --database 状态库 --output 结果文件`，字段语义见上一节“追溯工作流”。expected 是公开最小预期，产品不读取它；所有来源和选项独立支持，四个场景不替代全部边界测试。业务结果投影：batches、archives（不同附件摘要总数）、quarantine、affected、decisions、current_valid（无相关有效证书为 false）。数组顺序不影响检查，公开预期数组是子集，但空数组要求确实为空。每个场景在同一状态库上用两个独立进程执行，以检查重启和重复输入；业务投影保持一致，notifications 为本次新建通知数，第二次应为 0。

## 验收与正式展示

```sh
python3 acceptance/check.py --contracts-only
python3 acceptance/check.py
```

contracts-only 仅检查材料结构，不能当成功能验收。完整验收四个场景均应 PASS；每个场景独立临时目录，执行两次、每次进程超时 5 秒，八次总预算 40 秒，无固定 sleep。正式展示从干净状态执行基线测试和第二条命令，预算小于 90 秒；依赖和镜像预先准备，不能用旧日志代替运行。边界回归（重复邮件、同 Message-ID 内容变化、验收后补件、供应商隔离、历史时点查询）见 `tests/test_trace.py` 与 `tests/test_cli_workflow.py`，不修改预期来迁就实现。
