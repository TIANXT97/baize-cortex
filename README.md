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
| `CORTEX_ALLOW_ADMIN_SEED` | 是否放行 `POST /v1/admin/seed`，`1` / `true` / `on` 放行 | 拒绝（403） |
| `CORTEX_RESIDENT_DM_BUDGET` | 常驻层私聊注入的字符预算（含标题行，其中 `group_ok` 子集另受下一项限制） | 1500 |
| `CORTEX_RESIDENT_GROUP_BUDGET` | 常驻层群聊注入的字符预算，同时是私聊预算里 `group_ok` 规则的上限 | 600 |
| `CORTEX_MEMORY_MIGRATION` | 记忆文件自动外迁与夜间常驻层整理的开关：`off` / `dry_run` / `apply`，大小写与首尾空白忽略，非法值按 `dry_run` 处理并记一次警告 | `dry_run`：只出报告，文件和库（除运行记录）都不变 |
| `CORTEX_HERMES_HOME` | Hermes 主目录；记忆文件在 `<主目录>/memories/`，字数上限读 `<主目录>/config.yaml` 的 `memory.memory_char_limit` / `memory.user_char_limit`（读不到用 2200 / 1375） | `~/.hermes`（运行服务的用户的主目录） |

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
- `POST /v1/admin/seed` 会把预置示例卡重置为初始内容，已被夜间整理或手动改过的内容会被覆盖（覆盖前会存一份历史快照）。它**默认返回 403**：确需调用时，在服务环境里设置 `CORTEX_ALLOW_ADMIN_SEED=1`（`true` / `on` 同义，忽略大小写与首尾空白）并重启；已配置 `CORTEX_AGENT_TOKENS` 时还必须带对凭据。服务启动、定时任务和命令行都不会调用它

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

夜间慢波（推荐通过服务的 HTTP 接口触发。命令行入口 `python -m baize.core.consolidator` 和 MCP 的整理工具读取同一套 `CORTEX_LLM_URL` / `CORTEX_LLM_KEY` / `CORTEX_REASONING_MODEL` 配置，未配置时只做规则折叠）：

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

想先看今晚会改什么，把请求体换成 `{"dry_run": true}`：它会真的跑一遍分析，返回哪些卡会变、从什么改成什么、会留哪些墓碑，但不写库、不写冷归档。它不占用整理互斥，但分析时仍会调用 System 2（只读，会产生模型调用）。

整理调用模型期间不再持有服务的全局锁，召回和写入不会被挡住；同一时间只允许一次真实整理，重复触发会立即返回 `{"status": "busy", ...}`（HTTP 200），不排队也不重复整理。整理期间如果卡片或来源草稿行被改过，这一组会被跳过、留到下次，响应里多出 `skipped_changed`（每项含 `entity_id` 与 `reason`）。

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

#### 机器重启或休眠后的补跑

上面的 timer 开了 `Persistent=true`，机器停机跨过触发点时，开机后会立刻补跑。但 `baize-cortex.service` 是 `Type=simple`，进程起来不等于端口已经在听，光写 `After=` / `Wants=` 不够：补跑的慢波可能在服务就绪前就发请求，curl 报 `Failed to connect`（退出码 7），这一天就漏掉了，也不会自己重试。

用 drop-in 补上「先等健康检查、失败隔一段时间重试」，不改原单元文件：

```ini
# /etc/systemd/system/baize-cortex-slowwave.service.d/10-wait-and-retry.conf
[Unit]
# 3 小时内最多启动 4 次（含首次），主服务坏掉时不会无限重试
StartLimitIntervalSec=3h
StartLimitBurst=4

[Service]
# 最多等 5 分钟（60 次 × 5 秒）健康检查通过
ExecStartPre=/bin/sh -c 'i=0; until /usr/bin/curl -fsS -m 3 -o /dev/null http://127.0.0.1:8768/health; do i=$$((i+1)); if [ $$i -ge 60 ]; then echo "baize-cortex health check not ok after 5 min" >&2; exit 1; fi; sleep 5; done'
# HTTP 出错也算失败；连接被拒和临时错误重试 3 次。重复触发时服务返回 busy，不会重复整理
ExecStart=
ExecStart=/usr/bin/curl -sS --fail-with-body --retry 3 --retry-connrefused --retry-delay 20 -m 600 -X POST http://127.0.0.1:8768/v1/sleep/consolidate -H 'Content-Type: application/json' -d '{}'
TimeoutStartSec=2400
Restart=on-failure
RestartSec=10min

# /etc/systemd/system/baize-cortex-purge.service.d/10-wait-and-retry.conf
[Unit]
StartLimitIntervalSec=3h
StartLimitBurst=4

[Service]
ExecStartPre=/bin/sh -c 'i=0; until /usr/bin/curl -fsS -m 3 -o /dev/null http://127.0.0.1:8768/health; do i=$$((i+1)); if [ $$i -ge 60 ]; then echo "baize-cortex health check not ok after 5 min" >&2; exit 1; fi; sleep 5; done'
TimeoutStartSec=1800
Restart=on-failure
RestartSec=10min
```

- `ExecStartPre` 里的 `$$` 是 systemd 对 `$` 的转义，执行时会变回 `$`
- 启用了 `CORTEX_AGENT_TOKENS` 时，覆盖后的 `ExecStart` 里同样要加 `-H 'Authorization: Bearer <token>'`
- purge 直接读写数据库，等健康检查是为了不在服务启动流程中间插进去
- 改完执行 `systemctl daemon-reload`，用 `systemd-analyze verify <单元名>` 检查，`systemctl cat <单元名>` 应能看到 drop-in
- 慢波漏跑后想立刻补上：`systemctl start baize-cortex-slowwave.service`，然后在 `journalctl -u baize-cortex-slowwave.service` 里确认返回 `"status":"ok"`

### Hermes 插件

- 参考实现在 `baize/client/`（纯标准库：`plugin.py`、`outbox.py`、`breaker.py`、`recall.py`）。Hermes 实际加载的是插件目录下的入口文件，它 import `baize` 包并委托给 `CortexClient`
- 部署：把仓库里的 `baize/` 包放到插件进程能 import 到的位置（或在入口文件里把仓库路径加进 `sys.path`），入口文件放到 `<hermes_home>/plugins/cortex/`。入口文件约定读取 `CORTEX_URL`（服务地址，默认本机 8768 端口）、`CORTEX_TOKEN`（启用凭据时）和 `HERMES_HOME`（Outbox 文件放在 `<hermes_home>/outbox.jsonl`）
- 改了插件文件，或更新了 `baize/` 包，都要重启 Hermes 网关才生效
- 写失败兜底（Issue #105 PR2）需要入口把 `sync_turn` 的 `messages` 转给 client，补丁见 `deploy/hermes_plugin_entry_pr2.patch` 和上面「记忆文件自动外迁」一节
- 顺序：先升级并重启 Cortex 服务，再更新插件。新插件依赖服务端新增的接口，反序时群聊会 fail-closed，什么都不注入
- 常驻层（Issue #105）：入口文件的 Provider 里加一个委托方法即可，Hermes 在构建 system prompt 时调用一次，结果随 system prompt 冻结，改动下个会话生效：

  ```python
  def system_prompt_block(self) -> str:
      return self._client.resident_block(self._session_id or "") if self._client else ""
  ```

  `resident_block` 按会话的 `chat_type` 请求 `GET /v1/resident`（认不出按群聊），超时 2 秒；连不上、超时、非 2xx、返回异常都返回空串，不抛异常，`MEMORY.md` 底线照常生效。群聊只接受服务端回显 `scope=group` 的响应，并在本地再滤一遍：只留 `group_ok` 且通过安全校验的规则

### 常驻层（Issue #105）

常驻层放每个会话都要遵守的少量硬规则，开场由插件原样注入，不靠关键词召回，也不占 `MEMORY.md` 的字数上限。

- **存储**：`resident_rules`（`id`、`text`、`scope`、`priority`、`source`、`moved_from`、`batch_id`、`created_at`、`updated_at`、`status`）与 `resident_rule_history`（每次增、改、删各写一行旧值快照）。纯新增表，旧库启动时自动建表，不改任何已有表；删除是软删除（`status=retired`）。不复用 `pinned` 字段
- **scope**：只有 `dm`（默认，只在私聊注入）和 `group_ok`（群聊也注入）。`priority` 为 `hard` / `normal` / `low`
- **写入校验**（新增和修改都拦，纯函数在 `baize/core/resident.py` 的 `find_group_unsafe` / `validate_rule`）：标成 `group_ok` 的文本不能含 IP（v4/v6）、文件路径、主机名或域名加端口、URL、凭据样式（已知密钥前缀、`ou_` 加十六进制、`token=` 这类赋值、`password`、长随机串）与邮箱；`moved_from` 为 `USER.md` 的规则不能标 `group_ok`；单条最多 300 字符。命中返回 4xx，`detail` 写原因，`reasons` 给原因代码。普通中文句子里的斜杠（「私聊/群聊」）不算路径；误伤边界写在该文件的注释里
- **接口**（都要鉴权，写接口都支持 `dry_run`）：`GET /v1/resident?scope=dm|group`（返回 `text`、`rules` 与 `report`；认不出的 scope 一律按群聊，群聊只含 `group_ok`）、`GET /v1/resident/rules[?include_retired=true]`、`GET /v1/resident/rules/{id}`、`GET /v1/resident/rules/{id}/history`、`POST /v1/resident/rules`、`PATCH /v1/resident/rules/{id}`、`DELETE /v1/resident/rules/{id}`
- **预算**：私聊 1500 字符（其中 `group_ok` 最多 600），群聊 600 字符，可用上面两个环境变量覆盖；标题行与项目符号都算在内。超了不报错：按 `priority`（`hard` > `normal` > `low`）再按 `updated_at` 新到旧注入，放不下就停（低优先级不插队），剩下的写进 `report.omitted` 并打 warning 日志。合并与降级留给后续的夜间整理
- **导入**：`python -m baize.cortex_resident_import 清单.json`，默认只 `dry_run`，加 `--execute` 才写；执行前先整批 `dry_run`，有一条被拒就整批不写，重复执行安全。清单格式见 `docs/examples/resident_rules.example.json`，凭据读环境变量 `CORTEX_TOKEN`

### 记忆文件自动外迁（Issue #105 PR2）

Hermes 的 `MEMORY.md`（2200 字符）和 `USER.md`（1375 字符）有硬上限。cortex 在文件超过上限的 70% 时，把最早的条目外迁到常驻层或事实，降到 60% 以下。

- **开关**：`CORTEX_MEMORY_MIGRATION=off|dry_run|apply`，默认 `dry_run`。`dry_run` 只写运行记录，不改记忆文件，也不写常驻层、事实等其他表；`off` 连字数都不量。Hermes 主目录用 `CORTEX_HERMES_HOME` 指定，上限从它的 `config.yaml` 读。触发线 70%、目标线 60% 是 `baize/core/memory_migrator.py` 里的常量（`TRIGGER_PERCENT` / `TARGET_PERCENT`），按整数算：2200 对应 1540 / 1320，1375 对应 962 / 825
- **字数算法**与 Hermes 的 `_parse_entries` / `_char_count` 一致：按 `\n§\n` 分隔，每条 strip、去空、去重（保持顺序），再用 `\n§\n` join 后取 `len`，测试锁死
- **触发**：不加文件监视。以下请求顺手量一次，请求线程里只做 stat 加读文件，按 mtime / size 缓存，超线就起后台线程外迁，量字数或外迁的任何异常都不影响请求：`POST /v1/recall`（`scope=dm`）、`POST /v1/scratchpad/log`（同步对话）、`POST /v1/cortex/add` 里 `origin` 为镜像（`hermes_builtin_mirror`，插件 `on_memory_write` 只在 Hermes 写成功时才发）或写失败兜底（`hermes_memory_write_failed`）。群聊（`scope` 不是 `dm`、认不出）的 recall 不量。同一时间只跑一个外迁（进程内锁加库里的运行标记，标记超过 30 分钟算僵死可接管），一次外迁结束后同一文件至少隔 10 分钟再检查；`dry_run` 下文件没变就不重复出报告。夜间整理不触发外迁
- **挑哪些**：从最早的条目挑起，跳过开头带「【底线】」的条目，挑到剩下的字数低于目标线；全是底线或模型失败导致降不到目标时，能挑的都挑，报告写明
- **分类**：用跟夜间整理同一个 System 2 客户端逐条判断「规则」还是「知识」。模型报错、超时、返回解析不了的条目这次不动，下次再试；每次外迁最多调 10 次模型（`MAX_MODEL_CALLS`）
- **去向**：
  - 规则 → 常驻层（scope 固定 `dm`，`source=memory_migration`，`moved_from` 为原文件，`batch_id` 为本批），走 PR1 同一套校验。单条超 300 字符、含换行（常驻层按行注入，折行会改原文）、或写入会让常驻层超预算的，改按知识处理，报告写明原因（`rule_fallback`）。常驻层里已有一字不差的规则就复用，不重复写，回滚时也不会撤掉它
  - 知识 → 一条 `explicit_addition` 事实，原文不做 NFKC 归一（要求一字不差），`reason` 为 `moved_from=<文件>;batch=<batch_id>`，进待归卡队列，由夜间整理按 #95 / #104 的规则归卡
  - `USER.md` 来的：规则一律 `dm`（`moved_from=USER.md` 的规则不允许标 `group_ok`）；知识开头带 `proj:user_profile` 锚点，只进这张私有卡。卡不存在、已停用或不是私有时，不带锚点进队列并在报告里写 `anchor_note`，不会自动建卡
  - 库里已有内容一字不差的镜像事实（`hermes_builtin_mirror`）：新写一条带 `moved_from` 的事实，并把镜像那条标过期（软删除），所以夜里只会归一次；回滚时把镜像那条恢复。不复用旧行，是因为镜像行没有 `moved_from`，`USER.md` 的知识还需要锚点，改旧行的内容比新写加过期更难回滚
- **顺序与安全**（任何一步失败，记忆文件都不动，已写的库内副本会撤销）：① 备份到数据库同目录下的 `memory_backups/`，文件名带 `batch_id`，权限 0600，保留 14 天，过期的顺手清掉；② 写库并提交，逐条读回确认一字不差；③ 对 `<文件名>.lock` 加 `flock`（与 Hermes 同一把锁；锁文件不存在就按 0600 创建并 chown 成记忆文件的属主；用 `LOCK_NB` 轮询最多 30 秒，不会永远挂住），锁内重读，只删本批迁走且仍一字不差存在的条目，其余（包括 Hermes 这期间新写的）原样保留，在这期间被 Hermes 改动或删除的条目，对应的库内副本撤销；④ 写同目录临时文件再 `os.replace`，新文件 chown / chmod 回原来的 uid / gid / mode（cortex 以 root 运行时记忆文件属于另一个用户，不改回 Hermes 会读不了）；⑤ 写完重量字数写进报告
- **回滚**：`POST /v1/admin/memory-migration/rollback`，body `{"batch_id": "...", "dry_run": true}`（默认 `dry_run`）。常驻规则软删除（`status=retired`），事实标过期，被标过期的镜像事实恢复，然后在锁内把本批迁走的条目加回文件（已存在的不重复，插回原来的位置，不用备份整个覆盖，所以不会冲掉 Hermes 之后的新写入）。加回会超过上限时什么都不改（文件、库都不动），返回原因、`over_limit` 和 `backup_path`。只能撤回已执行（`apply`）且未撤回的批次
- **报告**：新表 `memory_migration_runs`（老库启动自动建表，不动已有表），每次外迁（含 `dry_run`、不到触发线的空跑）一行：触发来源、文件、模式、前后字数、备份路径，以及 `report_json` 里逐条的去向（常驻层 id 和 scope，或事实 id、锚点、队列）、跳过的条目和原因。`GET /v1/admin/memory-migration/runs?limit=&batch_id=&file=` 查最近几批。夜间整理的返回里带 `memory_migration`（当天的外迁摘要）和 `resident_budget`（见下）
- **写失败兜底**：Hermes 的 `memory` 工具超上限拒绝 add / replace 时不会触发 `on_memory_write`（它只在写成功后通知），拿不到内容。Hermes 每轮把本轮 `messages`（含工具调用和结果）传给插件的 `sync_turn`，`CortexClient.sync_turn` 收到后用 `failed_memory_writes` 识别 `success` 不是 true 且报错含 `would put memory at` / `would exceed the limit` 的 add / replace（含批量里的每个 add / replace，不含 patch），把内容改存进 cortex（`explicit_addition`，`reason=moved_from=<文件>;write_failed`），同时触发一次外迁检查。只在私聊会话做（认不出按群聊，不写）；同一条重复失败只存一次（进程内去重，服务端再按内容去重）；`USER.md` 来的失败内容同样只进私有卡。内容超过 2000 字符存不进去，记日志。入口文件没打补丁时其余功能照常，只是没有这条兜底
- **夜间常驻层整理**：常驻层私聊总字数超过预算（1500）的 90%，或 `group_ok` 部分超过群聊预算（600）的 90% 时，夜间整理先让模型合并相近的规则（只合并同 scope；合并结果走同一套校验；带 `USER.md` 来源的合并后仍是 `dm`；最多 2 次模型调用），还超就把优先级低、最久没改的规则降级成普通事实（`moved_from=resident_rules`；`USER.md` 来源的带私有卡锚点）。`priority=hard` 的既不合并也不降级。还超就沿用 PR1 的按优先级注入，只在报告里写明，不报错、不卡住。同样受 `CORTEX_MEMORY_MIGRATION` 管：`dry_run` 只报告「会合并 / 降级哪几条」（`would_merge` / `would_demote`），不改库
- **上线步骤**：
  1. 合并后先升级并重启 Cortex 服务（构建、重启由 NAS 侧维护者执行）。此时开关是默认的 `dry_run`，只出报告；日志里不会有任何记忆文件被改
  2. 观察一阵 `GET /v1/admin/memory-migration/runs` 和夜间整理返回里的 `memory_migration` / `resident_budget`，确认挑选和去向符合预期
  3. （可选，但没有它就没有写失败兜底）给插件入口打补丁：`cd <hermes_home> && patch -p1 < <仓库>/deploy/hermes_plugin_entry_pr2.patch`，只改一行，把 `sync_turn` 收到的 `messages` 转给 `self._client.sync_turn(..., messages=messages)`；补丁以现在线上入口为基准，先备份入口再 `patch --dry-run`；打完重启 Hermes 网关。必须先上新版服务和 `baize/` 包，再打补丁；补丁和 `on_memory_write` / `system_prompt_block` 不冲突
  4. 在服务的 systemd drop-in 里设 `Environment=CORTEX_MEMORY_MIGRATION=apply`，需要时再设 `Environment=CORTEX_HERMES_HOME=<主目录>`，重启服务。运行服务的用户要对 `<主目录>/memories/` 有读写权限（以非 root 运行时必须就是记忆文件的属主），并且对数据库所在目录有写权限（备份写在那里）
  5. 要撤回某一批：先 `dry_run` 看计划，再 `dry_run=false`

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

每个测试文件也可以单独运行，例如 `PYTHONPATH=. python tests/test_phase1_mvp.py`。仓库目前有 45 个测试文件，修改代码后请全部跑一遍。

---

## 📁 目录结构骨架

```text
baize-cortex/
├── docs/                      # 架构规范与理论文献
│   ├── ARCHITECTURE.md        # 白泽·Cortex 完整架构设计白皮书
│   ├── CHANGELOG.md           # 按 PR 记录的变更日志
│   ├── PEER_REVIEWS.md        # 三方异构红蓝军同行评审报告实录
│   └── examples/resident_rules.example.json # Issue #105: 常驻规则导入清单示例 (编造文本)
├── baize/                     # 核心源码骨架
│   ├── __init__.py
│   ├── cortex_purge.py        # 草稿流冷归档一键脚本 (默认 dry-run)
│   ├── cortex_resident_import.py # Issue #105: 常驻规则人审清单导入脚本 (默认 dry_run)
│   ├── core/                  # 核心引擎层
│   │   ├── living_state.py    # Layer 1: 项目活体状态卡管理器 (自动墓碑 / 历史快照 / 回滚)
│   │   ├── temporal_graph.py  # Layer 2: 双时态图谱与有效子图切片
│   │   ├── scratchpad.py      # Layer 0: 瞬态草稿流
│   │   ├── consolidator.py    # Layer 3: 夜间慢波睡眠合成引擎 (三段式: 锁内快照 / 锁外调模型 / 锁内复查回写, 整理互斥)
│   │   ├── normalization.py   # 内容规范化与幂等键 (客户端/服务端共用)
│   │   ├── supersession.py    # supersedes_id 校验与旧记录失效
│   │   ├── visibility.py      # Issue #72: 会话范围 (dm/group) 判定与活体卡群聊可见性
│   │   ├── resident.py        # Issue #105: 常驻层 (规则存储 / group_ok 写入校验 / 预算裁剪)
│   │   ├── memory_migrator.py # Issue #105 PR2: Hermes 记忆文件超线自动外迁 (字数算法 / 备份 / 锁内改写 / 回滚 / 运行记录)
│   │   ├── resident_compactor.py # Issue #105 PR2: 夜间常驻层合并与降级 (受 CORTEX_MEMORY_MIGRATION 管)
│   │   └── seeds.py           # 核心工程资产预置
│   ├── intelligence/          # 认知双系统与召回
│   │   ├── phase_detector.py  # System 1: 毫秒级相变探测器 (Drex-Max)
│   │   ├── batch_judge.py     # Issue #44: 夜间批量判研引擎 (System 2, 会话切片/状态机/confidence 门槛)
│   │   ├── ppr_engine.py      # 吸引子能量扩散检索引擎 (Personalized PageRank)
│   │   └── recall_router.py   # 统一认知召回路由 (2-gram 分词 / 加权打分 / PPR 游走 / 方案节点追溯 / 草稿兜底)
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
│   ├── test_issue83_seed_guard_and_system2_entries.py # Issue #83: seed 默认拒绝与三个整理入口共用 System 2 配置
│   ├── test_issue84_plan_node_trace.py # Issue #84: 方案节点追溯与兜底续走 (含群聊闸)
│   ├── test_issue87_consolidate_out_of_lock.py # Issue #87: 整理调模型不持全局锁、复查回写、整理互斥与 dry_run
│   ├── test_issue105_resident_layer.py # Issue #105: 常驻层 (校验 / 增查改删与历史 / 预算 / 群聊只给 group_ok / 插件取块 / 导入脚本)
│   ├── test_issue105_pr2_*.py      # Issue #105 PR2: 外迁 (字数 / 阈值 / 挑选 / 备份 / 权限属主 / 回滚)、触发点与写失败兜底、与 Hermes 抢锁并发、常驻层合并降级
│   ├── test_issue75_day_judge_log_only.py # Issue #75: 白天判定器只记日志 (CORTEX_DAY_JUDGE) 与草稿行软清理
│   ├── test_purge_before_date.py   # Issue #32: 草稿流时间窗归档与一键脚本
│   ├── test_purge_protect_phase_hints.py # Issue #34: 草稿归档默认豁免待固化记录
│   ├── test_phase_a_*.py           # Phase A: 迁移 / Outbox / cortex_add / 规范化 / 镜像 / 熔断 / 鉴权覆盖 / 配置沉淀
│   └── fixtures/normalization_vectors.json # 规范化 golden vectors
├── deploy/
│   └── hermes_plugin_entry_pr2.patch # Issue #105 PR2: 插件入口补丁 (把 messages 转给 client, 启用写失败兜底)
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
- [x] **Issue #83: seed 端点默认拒绝与整理入口统一 System 2** —— `POST /v1/admin/seed` 默认 403（`CORTEX_ALLOW_ADMIN_SEED` 为 `1` / `true` / `on` 才放行，仍先过鉴权）；HTTP 服务、命令行整理入口、MCP 整理工具共用 `llm_client_from_env()` 读取 System 2 配置
- [x] **Issue #84: 方案节点追溯与兜底续走** —— 问题里带已作废方案原话时，顺着 `decided_as` 边（含已失效）追溯到现役卡并返回现行方案；追溯不到继续走后面的兜底层；群聊视图沿用 #72 的闸
- [x] **Issue #87: 整理调模型挪出全局锁** —— 整理改为三段（锁内快照、锁外调模型、锁内复查回写），卡片或来源行被改过则整组跳过；整理专用非阻塞互斥，重复触发返回 `busy`；`dry_run` 同样走三段式
- [x] **Issue #105 PR1: 常驻层** —— 新表 `resident_rules` 与历史表、`/v1/resident*` 读写接口（写接口带 `dry_run`）、`group_ok` 写入校验（拦 IP / 路径 / 主机端口 / URL / 凭据，`USER.md` 来源不得标 `group_ok`）、私聊 1500 / 群聊 600 字符预算与 report、插件 `resident_block`（`system_prompt_block` 委托）、人审清单导入脚本；`MEMORY.md` / `USER.md` 自动外迁留给 PR2
- [x] **README 快速开始** —— 安装、环境变量、启动、定时任务、插件部署与升级说明
- [ ] **Issue #64: System 2 批量判研准召定线与慢波 candidate 闭环** —— 已完成 PR #65（只读评测引擎 judge_eval、布尔校验/标签保留契约硬化）与 PR #68（提示词定案与事实边界对齐）；NAS 侧黄金集复核已完成，PR #70（提示词 v3，草稿）实测三轮准确率 73%~74%，未达 95% 准入线，事实候选留存仍不稳；`CORTEX_BATCH_JUDGE` 继续保持 off，待准召达标后再受控启用
- [ ] **后续: 全量生产平滑割接** —— 现役存量数据提纯与全场景持续演化(原路线图中的 Phase 6 编号已被别名提纯占用,此项改为不编号待排期)

---

## 📜 开源协议

本项目基于 **Apache-2.0** 协议开源。
