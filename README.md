# 🏛️ 白泽·Cortex (Bai Ze Cortex)

> **工程命名**：`baize-cortex`  
> **代号**：`Baize-Phoenix`（白泽·涅槃）  
> **定位**：仿生认知双轨 · 双时态状态机 · 嵌入式抗漂移 AI Agent 长期记忆中枢

---

## 📖 项目背景与哲学

白泽 1.x 证明了轻量自托管记忆系统的可行性，但在面对长周期、高频排障、复杂决策推翻等真实工程场景时，暴露了以“扁平追加事实条目（Flat Append-Only）”为核心的结构性缺陷：
1. **语义级僵尸数据**：新旧方案在库中平权共存，无法物理隔离已作废的草案；
2. **时效池通胀雪崩**：短期高频排障碎屑冲垮时效记忆；
3. **门控机械误杀**：死板规则将合法短查询吞噬；
4. **单步向量缺乏深度联想**：无法完成人脑式的“模式补全（Pattern Completion）”与情境还原。

**白泽·Cortex（baize-cortex）彻底摆脱“字典式记录”，转向“仿生认知中枢”**。借鉴现代计算神经科学（全息脑理论、海马-新皮层双阶段收敛、慢波睡眠突触修剪）与 2025~2026 年顶尖学术前沿（HippoRAG 2、Graphiti、MOSAIC、RaMem），构建一套具备**抗漂移、自愈合、永不膨胀**的下一代认知底座。

---

## 🏗️ 核心架构：四层物理正交模型

系统依然严格恪守**单文件嵌入式（SQLite WAL 模式）**，运行时常驻内存从容控制在 500MB~1GB 黄金区间，零外部重量级中间件。

```text
 ┌────────────────────────────────────────────────────────┐
 │                    Agent / 宿主 交互层                  │
 └───────────────────────────┬────────────────────────────┘
                             │
            ┌────────────────┴────────────────┐
       【白天交互链路 (微秒级零损耗)】       【夜间异步静默链路】
            │                                 │
            ▼                                 ▼
 ┌───────────────────────────┐     ┌───────────────────────────┐
 │ Layer 0: 瞬态草稿流        │ ──► │ Layer 3: 慢波合成睡眠     │
 │ (Episodic Scratchpad)     │     │ (Dream Consolidation)     │
 │ 纯追加时序日志，零深层写入 │     │ 抽象折叠/拓扑演化/冷归档  │
 └───────────────────────────┘     └─────────────┬─────────────┘
                                                 │
                                                 ▼
 ┌───────────────────────────┐     ┌───────────────────────────┐
 │ Layer 1: Living State     │ ◄───│ Layer 2: Bi-Temporal Graph│
 │ (项目活体状态卡 · 现役唯一)│     │ (双时态实体拓扑图)        │
 │ 一项目一卡，终审即时覆写   │     │ valid_at / invalid_at     │
 └───────────────────────────┘     └───────────────────────────┘
```

### 1. Layer 0: 瞬态草稿流 (Episodic Scratchpad)
- **职责**：承载白天的一切高频碎屑、排障数据、调试报错、普通闲聊。
- **机制**：纯时序追加写，零向量化，零门控拦截，确保白天交互绝对零延迟。

### 2. Layer 1: 项目活体状态卡 (Living State Vault)
- **职责**：系统最高优先级的现役真理仓，严格收敛于 20~30 个核心工程实体。
- **机制**：一项目一文档，包含现役定案（Current Decision）、状态机标识与被推翻方案墓碑（Tombstones）。查询工程方案直通主键，**物理根除历史僵尸方案**。

### 3. Layer 2: 双时态实体拓扑图 (Bi-temporal Graph)
- **职责**：实体多跳关联与演化追踪。
- **机制**：单文件 SQLite 原生邻接表与双时态字段（`valid_at` / `invalid_at`）。检索时使用轻量 Personalized PageRank (PPR) 在**有效子图**上执行概率能量扩散，实现人脑吸引子（Attractor Basin）式的模糊意图自发收敛。

### 4. Layer 3: 慢波合成睡眠 (Dream Consolidation)
- **职责**：系统的夜间去噪与认知淬火引擎。
- **机制**：凌晨无监督拉网回放，将草稿流中具备相变特征的议题折叠抽象进 Living State，将 90% 瞬态日志无损冷归档压缩存盘，保持活跃记忆库容恒定平衡。

---

## ⚡ 双系统协同：System 1 (直觉) + System 2 (深思)

- **系统一（System 1 · 毫秒级决策哨兵）**：
  采用专用的微型决策模型（Drex-Max，推断延迟 ~24ms），在白天交互流中充当第一道岗哨，实时嗅探相变信号（拍板、推翻、立项）并分流工程意图。
- **系统二（System 2 · 深度推理模型）**：
  采用具备深度推理能力的大语言模型，在夜间睡眠回放中对全天复杂上下文做全局对账、冲突仲裁与白皮书级提炼。

---

## 🚀 快速开始

### 环境要求

- Python 3.12（测试与运行均使用 3.12，其它版本未验证）
- 依赖只有 `requirements.txt` 里的 fastapi、uvicorn、pydantic、httpx（httpx 供 TestClient 使用）
- 存储只用 SQLite，不需要 Redis、Kafka 等中间件

### 安装

```bash
git clone https://github.com/TIANXT97/baize-cortex.git
cd baize-cortex
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 配置（环境变量）

全部可选，进程启动时读取，修改后需要重启对应进程。

**Cortex 服务进程**（`uvicorn` 启动的那个）：

| 变量 | 作用 | 未设置时 |
|---|---|---|
| `CORTEX_DAY_JUDGE` | 白天判定器的结果能否写进正式记忆。只有等于 `apply` 才保留白天的 approval / rejection | `log_only`：approval / rejection 降为普通行，原判定记入 `reason` 列；`GET /health` 的 `day_judge` 字段回显当前模式 |
| `CORTEX_BATCH_JUDGE` | 夜间批量判研总开关，`1` / `true` / `on` 开启 | 关闭 |
| `CORTEX_LLM_URL`、`CORTEX_LLM_KEY` | System 2（慢波合成、批量判研、candidate 核实）使用的 OpenAI 兼容网关地址与密钥，需同时设置 | 慢波合成降级为正则规则；批量判研与 candidate 核实不运行 |
| `CORTEX_REASONING_MODEL` | System 2 使用的模型名 | 使用代码内置默认值（见 `baize/intelligence/phase_detector.py`） |
| `CORTEX_AGENT_TOKENS` | 凭据表，格式 `token:agent_id,token2:agent_id2` | 不校验，所有写入归属 `default` |

**插件进程**（Hermes 网关，加载 `baize/client/` 的那一侧）：

| 变量 | 作用 | 未设置时 |
|---|---|---|
| `CORTEX_SYSTEMONE_DREX_URL`、`CORTEX_SYSTEMONE_DREX_KEY` | System 1 第一道门神的地址与密钥 | 跳过该门神 |
| `CORTEX_SYSTEMONE_OPENJEV_URL`、`CORTEX_SYSTEMONE_OPENJEV_KEY` | System 1 第二道门神（模糊区复判）的地址与密钥 | 跳过该门神 |
| `CORTEX_SYSTEMONE_PROXY` | 门神请求使用的代理 | 直连 |
| `CORTEX_URL`、`CORTEX_TOKEN`、`HERMES_HOME` | 参考插件入口文件读取：Cortex 服务地址、凭据（服务端启用 `CORTEX_AGENT_TOKENS` 时必填）、Outbox 所在目录 | 见「Hermes 插件」一节 |

两道门神都不可用时，白天判定退回本地关键词规则，不阻塞写入。示例（地址、模型名、密钥都是占位符，不要写进仓库）：

```bash
export CORTEX_LLM_URL="<System 2 网关地址>"
export CORTEX_LLM_KEY="<密钥>"
export CORTEX_REASONING_MODEL="<模型名>"
export CORTEX_AGENT_TOKENS="<token>:<agent_id>"
```

长期运行时，把这些变量放进权限为 600 的 systemd `EnvironmentFile`，不要放进命令行或脚本。

### 启动

数据库和冷归档都建在进程的**当前工作目录**下，路径不能用环境变量修改，所以建议在单独的数据目录里启动：

```bash
export BAIZE_HOME="$PWD"              # 仓库根目录
mkdir -p ../baize-data && cd ../baize-data
PYTHONPATH="$BAIZE_HOME" "$BAIZE_HOME/.venv/bin/uvicorn" baize.server.app:create_app --factory --host 127.0.0.1 --port 8768
```

另开一个终端自检：

```bash
curl -s http://127.0.0.1:8768/health
```

返回 `status: ok`，并带有版本号、`day_judge` 模式和各模块状态。

- 数据库：`baize.db`（WAL 模式，会同时出现 `-wal`、`-shm` 文件）；冷归档：`cold_archive/*.jsonl.gz`（首次归档时才创建）
- 启动时自动建表并做增量迁移；有存量数据且需要加列时，会先备份为 `baize.db.bak-<时间戳>`
- MCP stdio 入口：`python -m baize.server.mcp_server --db baize.db`（手写 JSON-RPC 2.0）
- 服务只应监听本机回环地址。未设置 `CORTEX_AGENT_TOKENS` 时所有 `/v1/*` 端点都不校验凭据，不要把端口暴露到本机以外；设置后缺少凭据返回 `401`、凭据无效返回 `403`
- `POST /v1/admin/seed` 会把预置示例卡重置为初始内容，已被夜间整理或手动改过的内容会被覆盖（覆盖前会存一份历史快照），生产环境不要随意调用

### systemd 与定时任务

服务（路径、用户都是占位）：

```ini
# /etc/systemd/system/baize-cortex.service
[Unit]
Description=Baize Cortex
After=network.target

[Service]
User=<运行用户>
WorkingDirectory=/srv/baize-data
Environment=PYTHONPATH=/opt/baize-cortex
EnvironmentFile=/etc/baize-cortex/env
ExecStart=/opt/baize-cortex/.venv/bin/uvicorn baize.server.app:create_app --factory --host 127.0.0.1 --port 8768
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

夜间慢波（通过服务的 HTTP 接口触发，这样会用到服务已配置的 System 2；命令行入口 `python -m baize.core.consolidator` 不连接 System 2，只做规则折叠）：

```ini
# /etc/systemd/system/baize-cortex-slowwave.service
[Service]
Type=oneshot
ExecStart=/usr/bin/curl -sS -m 600 -X POST http://127.0.0.1:8768/v1/sleep/consolidate -H 'Content-Type: application/json' -d '{}'
# 启用了 CORTEX_AGENT_TOKENS 时，再加：-H 'Authorization: Bearer <token>'

# /etc/systemd/system/baize-cortex-slowwave.timer
[Timer]
OnCalendar=*-*-* 18:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

想先看今晚会改什么，把请求体换成 `{"dry_run": true}`：它会真的跑一遍分析，返回哪些卡会变、从什么改成什么、会留哪些墓碑，但不写库、不写冷归档。

草稿流清理（默认只列出将被归档的记录，加 `--execute` 才先写冷归档再删除；带待固化标记的行默认豁免）：

```ini
# /etc/systemd/system/baize-cortex-purge.service
[Service]
Type=oneshot
WorkingDirectory=/opt/baize-cortex
ExecStart=/opt/baize-cortex/.venv/bin/python -m baize.cortex_purge --db /srv/baize-data/baize.db --archive-dir /srv/baize-data/cold_archive --days 30 --execute

# /etc/systemd/system/baize-cortex-purge.timer
[Timer]
OnCalendar=Sun *-*-* 20:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

### Hermes 插件

- 参考实现在 `baize/client/`（纯标准库：`plugin.py`、`outbox.py`、`breaker.py`、`recall.py`）。Hermes 实际加载的是插件目录下的入口文件，它 import `baize` 包并委托给 `CortexClient`
- 部署：把仓库里的 `baize/` 包放到插件进程能 import 到的位置（或在入口文件里把仓库路径加进 `sys.path`），入口文件放到 `<hermes_home>/plugins/cortex/`。入口文件约定读取 `CORTEX_URL`（服务地址，默认本机 8768 端口）、`CORTEX_TOKEN`（启用凭据时）和 `HERMES_HOME`（Outbox 文件放在 `<hermes_home>/outbox.jsonl`）
- 改了插件文件，或更新了 `baize/` 包，都要重启 Hermes 网关才生效
- 顺序：先升级并重启 Cortex 服务，再更新插件。新插件依赖服务端新增的接口，反序时群聊会 fail-closed，什么都不注入

### 升级

```bash
cd /opt/baize-cortex
git pull --ff-only origin main
.venv/bin/pip install -r requirements.txt

# 备份数据库（在线一致性备份，包含 WAL 里的内容）
.venv/bin/python -c "import sqlite3,time; s=sqlite3.connect('/srv/baize-data/baize.db'); d=sqlite3.connect('/srv/baize-data/baize.db.bak-'+time.strftime('%Y%m%dT%H%M%S')); s.backup(d); d.close(); s.close()"

sudo systemctl restart baize-cortex
curl -s http://127.0.0.1:8768/health
```

新表（例如卡片历史表）在服务启动时自动创建；迁移只做增量加法，回退到旧版本代码时旧版本不认识新表，但可以照常运行。

### 运行测试

在仓库根目录、已激活 venv 的环境里：

```bash
for f in tests/test_*.py; do PYTHONPATH=. python "$f" || echo "FAILED: $f"; done
```

每个测试文件也可以单独运行，例如 `PYTHONPATH=. python tests/test_phase1_mvp.py`。

---

## 📁 目录结构骨架

```text
baize-cortex/
├── docs/                      # 架构规范与理论文献
│   ├── ARCHITECTURE.md        # 白泽·Cortex 完整架构设计白皮书
│   ├── CHANGELOG.md           # 按 PR 记录的变更日志
│   └── PEER_REVIEWS.md        # 三方异构红蓝军同行评审报告实录
├── baize/                     # 核心源码骨架
│   ├── __init__.py
│   ├── cortex_purge.py        # 草稿流冷归档一键脚本 (默认 dry-run)
│   ├── core/                  # 核心引擎层
│   │   ├── living_state.py    # Layer 1: 项目活体状态卡管理器 (自动墓碑 / 历史快照 / 回滚)
│   │   ├── temporal_graph.py  # Layer 2: 双时态图谱与有效子图切片
│   │   ├── scratchpad.py      # Layer 0: 瞬态草稿流
│   │   ├── consolidator.py    # Layer 3: 夜间慢波睡眠合成引擎
│   │   ├── normalization.py   # 内容规范化与幂等键 (客户端/服务端共用)
│   │   ├── supersession.py    # supersedes_id 校验与旧记录失效
│   │   ├── visibility.py      # Issue #72: 会话范围 (dm/group) 判定与活体卡群聊可见性
│   │   └── seeds.py           # 核心工程资产预置
│   ├── intelligence/          # 认知双系统与召回
│   │   ├── phase_detector.py  # System 1: 毫秒级相变探测器 (Drex-Max)
│   │   ├── batch_judge.py     # Issue #44: 夜间批量判研引擎 (System 2, 会话切片/状态机/confidence 门槛)
│   │   ├── ppr_engine.py      # 吸引子能量扩散检索引擎 (Personalized PageRank)
│   │   └── recall_router.py   # 统一认知召回路由 (2-gram 分词 / 加权打分 / PPR 游走 / 草稿兜底)
│   ├── storage/               # 存储底座 (SQLite WAL)
│   │   ├── schema.sql         # 四层正交物理数据库 Schema
│   │   ├── migrations.py      # Phase A 增量迁移 (可重复执行、迁移前备份)
│   │   └── db.py              # 连接与 PRAGMA 初始化
│   ├── client/                # cortex 插件参考实现 (纯标准库)
│   │   ├── outbox.py          # 可靠 Outbox 投递层
│   │   ├── plugin.py          # cortex_add、on_memory_write 镜像钩子与按 chat_type 过滤的群聊记忆闸
│   │   └── breaker.py         # 错误签名归一化与熔断
│   └── server/                # 服务接入 (FastAPI + MCP)
│       ├── app.py             # REST 端点
│       └── mcp_server.py      # MCP stdio 服务 (JSON-RPC 2.0)
├── tests/                     # 验证套件
│   ├── test_bitemporal.py          # 双时态一致性
│   ├── test_phase1_mvp.py          # 活体卡与草稿流 MVP
│   ├── test_phase2_mcp_graph.py    # 图谱、PhaseDetector、MCP 协议
│   ├── test_phase3_consolidation.py # 慢波合成与冷归档
│   ├── test_phase4_recall_router.py # 认知召回路由与端到端
│   ├── test_phase5_hardening.py    # Phase 5: 承重加固与模型接入回退
│   ├── test_phase6_semantic_alias.py # Phase 6: 别名提纯与语义召回
│   ├── test_phase7_systemone_gates.py # Phase 7: SystemOne 双门神
│   ├── test_phase8_consolidation_closure.py # Phase 8: 慢波合成收口与管理接口
│   ├── test_explicit_addition_consolidation.py # Issue #25: 显式沉淀慢波固化
│   ├── test_explicit_addition_recall.py # Issue #25: 未固化事实的兜底召回
│   ├── test_recall_scoring.py      # Issue #32: 2-gram 分词、打分排序与命中回写
│   ├── test_recall_single_gram_gate.py # Issue #44 红线: 门槛回撤锁定与别名库通用词黑名单
│   ├── test_batch_judge_candidates.py # Issue #44 PR4: candidate 候选流与批量判研状态机
│   ├── test_issue72_group_memory_gate.py # Issue #72: 群聊记忆闸 (chat_type / 可见性 / scope 过滤)
│   ├── test_issue78_tombstone_history_rollback.py # Issue #78: 自动墓碑、卡片历史、回滚与 consolidate dry_run 预览
│   ├── test_issue75_day_judge_log_only.py # Issue #75: 白天判定器只记日志 (CORTEX_DAY_JUDGE) 与草稿行软清理
│   ├── test_purge_before_date.py   # Issue #32: 草稿流时间窗归档与一键脚本
│   ├── test_purge_protect_phase_hints.py # Issue #34: 草稿归档默认豁免待固化记录
│   ├── test_phase_a_*.py           # Phase A: 迁移 / Outbox / cortex_add / 规范化 / 镜像 / 熔断 / 鉴权覆盖 / 配置沉淀
│   └── fixtures/normalization_vectors.json # 规范化 golden vectors
├── README.md
├── LICENSE
└── requirements.txt
```

---

## 🗺️ 演进路线图 (Roadmap)

- [x] **Phase 0: 规范奠基** —— 完成架构白皮书、同行评审归档与 Schema 冻结
- [x] **Phase 1: 活体状态单卡 (MVP)** —— 建立 Living State 引擎,实现核心项目秒回与墓碑隔离
- [x] **Phase 2: 双时态图谱与 PPR 扩散** —— 实现有效子图切片、PPR 能量扩散、PhaseDetector 相变嗅探与 MCP stdio 服务
- [x] **Phase 3: 慢波睡眠回放** —— 实现慢波合成引擎、规则降级、无损 gzip 冷归档与热表清退
- [x] **Phase 4: 认知召回中枢** —— 实现 `CognitiveRecallRouter`(活体卡直达 / PPR 游走)、核心工程资产预置、`POST /v1/recall` 与 MCP `baize_recall`
- [x] **Phase 5: 承重架构加固与生产可靠性** —— 修复 F1~F9 核心承重墙（唯一归档防覆盖、授权批准门、图文双写同步、双时态生效断言），实现 System 1 & System 2 真实模型接入与 Hermes 插件契约贯通
- [x] **Phase 6: 别名提纯与语义召回** —— 慢波合成解析 `alias:`、别名并集合并,召回路由支持双向匹配与 `DECISION_TEXT_MATCH` 兜底
- [x] **Phase 7: System 1 双门神** —— `PhaseDetector` 切换为 Drex-Max / OpenJev 的 SystemOne 协议,全部失败时 Fail-open 降级本地规则
- [x] **Phase 8: 慢波合成收口** —— 全角冒号清洗、无锚点定案兜底、黑户展示名转正,新增 `backfill-display-names` 与 `purge-scratchpad` 管理接口
- [x] **Phase A: 记忆外溢通道与可靠投递** —— Schema 增量迁移、`POST /v1/cortex/add` 幂等写入、Outbox 投递、memory 镜像钩子与错误签名熔断
- [x] **Issue #44: 相态判研去关键词化** —— candidate 候选流（高 noul 事实/疑似计划经 System 2 核实后固化,严禁改写定案）、judge_status 批量判研状态机（会话切片/ID 校验/重试上限/failed 终态,`CORTEX_BATCH_JUDGE` 默认 off）、门神 401 鉴权熔断、配置改动事后沉淀通道与召回门槛红线纠偏
- [x] **Issue #56: 批量判研二审排险** —— PR 1（切批空批防护、步骤异常隔离）、PR 2（candidate 事实/定案分流）、PR 3（插件 candidate 透传与标准词表绑定、薄壳委托）全量闭环
- [x] **Issue #72: 群聊记忆闸** —— 只认 Hermes 传入的 `chat_type`（`dm` 之外一律按群聊）、活体卡 `metadata.visibility=group_ok` 才可在群里出现（缺省私人）、`/v1/recall` 新增 `scope` 与回显、群聊不出卡片 facts/草稿/最近对话、`POST /v1/admin/living-state/visibility` 管理入口；部署顺序为先服务端后插件
- [x] **Issue #75: 白天判定器默认只记日志** —— 服务端 `CORTEX_DAY_JUDGE`（只有 `apply` 才让白天 approval / rejection 写进正式记忆，默认 `log_only` 降为普通行并把原判定记入 `reason`）、`/health` 回显 `day_judge`、夜间整理跳过已过期的定案/推翻行、`POST /v1/admin/expire-scratchpad` 草稿行软清理（默认 `dry_run`）
- [x] **Issue #78: 墓碑、卡片历史与回滚** —— 定案被新定案替换时自动给旧定案留墓碑；新表 `living_state_history` 在每次卡片内容变化前存旧快照；`POST /v1/admin/living-state/rollback`（默认 `dry_run`、保留当前可见性、可再回滚回去）；`POST /v1/sleep/consolidate` 的 `dry_run` 改为真预览（不写库、不写冷归档）
- [x] **README 快速开始** —— 安装、环境变量、启动、定时任务、插件部署与升级说明
- [ ] **Issue #64: System 2 批量判研准召定线与慢波 candidate 闭环** —— 已完成 PR #65（只读评测引擎 judge_eval、布尔校验/标签保留契约硬化）与 PR #68（提示词定案与事实边界对齐）；NAS 侧黄金集复核已完成，PR #70（提示词 v3，草稿）实测三轮准确率 73%~74%，未达 95% 准入线，事实候选留存仍不稳；`CORTEX_BATCH_JUDGE` 继续保持 off，待准召达标后再受控启用
- [ ] **后续: 全量生产平滑割接** —— 现役存量数据提纯与全场景持续演化(原路线图中的 Phase 6 编号已被别名提纯占用,此项改为不编号待排期)

---

## 📜 开源协议

本项目基于 **Apache-2.0** 协议开源。
