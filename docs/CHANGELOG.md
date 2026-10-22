# 变更日志

按合并时间倒序记录。每个功能 PR 合并后，补一个 `docs/*` 分支同步本文件、`README.md` 与 `docs/ARCHITECTURE.md`，同样走 PR 合并。

## 2026-10-22

### Phase A：记忆外溢通道与可靠投递（PR #23，Issue #22）
- 三表（`scratchpad_log` / `living_states` / `graph_edges`）增量加列：`idempotency_key`、`valid_at`、`invalid_at`、`ingested_at`、`expired_at`、`supersedes_id`、`pinned`、`hit_count`、`last_recalled_at`、`agent_id`；`scratchpad_log` 另增 `category`、`reason`。迁移可重复执行，迁移前备份，存量分批回填。
- 新增 `POST /v1/cortex/add`，`POST /v1/scratchpad/log` 支持 `idempotency_key`；命中返回 `200` + `deduplicated: true`。
- 新增 `CORTEX_AGENT_TOKENS`：未配置时不校验、归属 `default`；配置后 `401/403`，并忽略请求体里的信任字段。
- `supersedes_id` 同事务校验并使旧记录失效，同时写 `superseded_by` 边。
- 新增 `baize/client/`（纯标准库）：Outbox（Windows `msvcrt` / POSIX `fcntl`）、`cortex_add`、`on_memory_write` 镜像钩子、错误签名熔断。
- 新增 `baize/core/normalization.py` 与 golden vectors `tests/fixtures/normalization_vectors.json`。
- 约定：时间戳统一 `YYYY-MM-DD HH:MM:SS`，不使用带 `T` 的 ISO 格式，避免破坏 `v_active_graph_edges` 的字符串比较。
- 未验证：POSIX 锁路径与 `0600` 权限断言（开发机为 Windows）；熔断端到端场景使用模拟的 `memory` 工具。

## 2026-10-21

### Phase 8：慢波合成收口（PR #20，Issue #19）
- 定案文本清洗同时去除半角 `:` 与全角 `：`。
- 授权批准但无 `proj:` 锚点的记录不再滞留：归入同会话最近实体，其次 30 分钟内最近更新的 ACTIVE 实体，兜底 `proj:general_decisions`。
- 展示名等于 `entity_id` 的“黑户”可转正，已有良好名称不被覆盖。
- 新增 `POST /v1/admin/backfill-display-names` 与 `POST /v1/admin/purge-scratchpad`，均默认 `dry_run`；真实清洗先写入 `cold_archive/purged_*.jsonl.gz` 再删除。
- 同步调整 Phase 5 测试：无锚点批准现在会被消费，不再留在热表。

### 修复：启动崩溃（PR #18）
- `app.py` 对齐 `llm_settings_from_env` 的 3 元组返回值，修复服务启动崩溃。

### Phase 7：System 1 双门神（PR #17，Issue #16）
- `PhaseDetector` 切换为 SystemOne 协议：先问 Drex-Max，`noul` 落在 (0.4, 0.6) 时交 OpenJev 复判，全部失败降级本地规则（Fail-open）。
- 新增环境变量 `CORTEX_SYSTEMONE_DREX_URL/KEY`、`CORTEX_SYSTEMONE_OPENJEV_URL/KEY`、`CORTEX_SYSTEMONE_PROXY`；`CORTEX_DECISION_MODEL` 不再使用。
- `OpenAICompatClient` 保留，仅服务夜间慢波合成。

### Phase 6：别名提纯与语义召回（PR #15，Issue #14）
- 慢波合成解析 `alias:` 行，别名与已有 `metadata.aliases` 取并集。
- 召回路由增加双向匹配、语气助词剥离，以及 `DECISION_TEXT_MATCH` 兜底路由。

### 文档：Phase 5 白皮书与插件契约（PR #13）
- 补充承重架构加固（F1~F9）与 Hermes 插件契约规范。

### Phase 5：承重架构加固（PR #12，Issue #11）
- 唯一命名归档防覆盖、先落盘后清退、仅清退已折叠记录。
- 授权批准门 `_is_authoritative`。
- 图文双写同步与双时态生效断言（`valid_at <= datetime('now')`）。
- System 1 / System 2 真实模型接入。

### 文档（PR #9、#10）
- 新增项目级 `CLAUDE.md`；README 同步 Phase 0–4 进度与目录结构。

### Phase 4：认知召回中枢（PR #8）
- `CognitiveRecallRouter`（活体卡直达 / PPR 游走）、核心工程资产预置、`POST /v1/recall` 与 MCP `baize_recall`。

### Phase 3：慢波睡眠（PR #6）
- 慢波合成引擎、规则降级、无损 gzip 冷归档与热表清退。

### Phase 2：双时态图谱与 MCP（PR #4）
- MCP stdio 服务、图谱生命周期端点、`PhaseDetector`。

### Phase 1：核心服务栈（PR #2）
- 活体状态卡、草稿流、REST 与 MCP 接口（MVP）。
