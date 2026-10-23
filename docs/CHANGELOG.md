# 变更日志

按合并时间倒序记录。每个功能 PR 合并后，补一个 `docs/*` 分支同步本文件、`README.md` 与 `docs/ARCHITECTURE.md`，同样走 PR 合并。

## 2026-10-23

### 修复：草稿归档默认豁免待固化记录（PR #35，Issue #34）
- 现象：美西分身在生产副本上对 PR #33 做 `before_date=2026-10-22 00:00:00` 的归档演练，匹配出的 61 条里混入一条 `phase_hint='approval'` 的权威定案（id=35）。`find_for_purge` 只按 `before_id`、`role`、`before_date` 过滤，完全没有 `phase_hint` 维度，积压超过窗口的定案会被常规清理静默删除，慢波合成再也读不到。
- 修复：`find_for_purge` 默认追加 `phase_hint IS NULL`，`ScratchpadManager`、`purge_scratchpad`、`PurgeIn` 与 CLI 新增 `include_pending_phase`（CLI 为 `--include-pending-phase`），默认关闭。
- 豁免范围是所有 `phase_hint` 非空的记录，比工单列出的 `approval`、`explicit_addition` 多了 `rejection`：慢波合成读取的正是这些记录，`rejection` 是推翻旧方案的依据，删掉同样丢信息。
- 报告新增 `include_pending_phase` 与 `protected_pending`，dry-run 即可看到被保护的条数。
- 新增测试 `test_purge_protect_phase_hints.py`（6 条），其中一条验证默认归档之后 approval 仍能被下一次慢波合成固化；`test_purge_before_date.py` 中归档显式沉淀记录的用例改为显式打开开关。
- 夭夭已在生产环境实测并完成部署。

### 召回精度与时效排序、草稿流时间窗归档（PR #33，Issue #32）
- **召回漏召回**：连续中文原先被整体当作一个 token，`recall("飞书卡片")` 无法命中『飞书流式卡片』。分词改为「原词 + 中文 2-gram」双通道，不需要用户手动加空格。
- **召回错召回**：原先按 `list_active_states()` 的物理顺序遍历、首个命中即返回，`recall("飞书 流式")` 会被排在第一的模具知识库抢占。改为对全部现役卡打分，`Score = 3×display_name + 2×alias + 1×decision/facts + 2×命中 token 数 + max(0, 1 − 0.05×距上次更新天数)`，按 Score 降序、同分取 `updated_at` 新者、再按 `entity_id` 取前 `top_k`。每条 `living_states` 带 `score`。
- **命中门槛**：`DECISION_TEXT_MATCH` 要求至少一个原词命中或至少两个不同的 2-gram 命中，单个 2-gram 巧合不足以入选。这一条超出批复的字面公式，是为了让 2-gram 引入后不产生通用词噪声而加的，已在白皮书 §3.3 写明。
- **时效回写**：被返回的活体卡 `hit_count += 1`、`last_recalled_at` 写入当前 UTC，不改 `updated_at`；未命中与草稿兜底不回写；回写失败回滚并忽略，召回照常返回。Phase A 预留的这两列从此启用，`pinned` 仍为预留。
- **草稿流归档**：`POST /v1/admin/purge-scratchpad` 新增 `before_date`（`YYYY-MM-DD HH:MM:SS`，带 `T` 或非法日期返回 `422`），与 `before_id`、`role` 为 AND 关系；`dry_run` 默认 true 并返回完整 `ids` 清单；真实执行先写 `cold_archive/purged_*.jsonl.gz` 再分块删除。冷归档新增 `category`、`reason` 两个字段。
- **一键脚本**：`python -m baize.cortex_purge --days 30`，默认 dry-run，`--execute` 才真正归档删除。
- **决策记录**：`CORTEX_AGENT_TOKENS` 暂缓启用（总监裁定选 A），触发条件与启用步骤已写入白皮书 §6.2，本次不改配置。
- 新增测试：`test_recall_scoring.py`、`test_purge_before_date.py`；`test_prefetch_contract_and_fusion.py` 的只读断言改为「草稿与活体卡内容不变」，因为命中回写会改变 `total_changes`。
- 未验证：没有在生产副本上复现；`PRAGMA integrity_check` 只在测试库上验证；`hit_count` 的写入会让每次召回多一次短事务，未做压测。

### 修复：对齐 Hermes MemoryProvider 字符串契约与召回指示器（PR #31，Issue #30）
- 现象：美西生产环境排查发现，`prefetch()` 返回 `List[Dict]` 违反 Hermes `MemoryProvider -> str` 契约，导致消费端 `result.strip()` 抛出 `AttributeError`，该异常被 `turn_context.py` 中的 `suppress` 静默吞噬，全库记忆自动注入计数恒为 0。
- 契约对齐：`prefetch()` 对齐基类标准关键字签名 `(self, query: str, *, session_id: str = "") -> str`，将召回条目格式化为自然文本行，无命中返回空字符串 `""`。
- 召回透出：新增 `recall_status()` 钩子，返回 `RecallStatus(provider_label="baize-cortex", count=N)`，点亮飞书前端感知。
- 群聊隔离：加固 `fail-closed` 闸门，识别 `:group:` 与飞书群聊 `oc_` 前缀，恒定阻断记忆外溢。
- 客户端库：`CortexClient.prefetch` 支持 `as_text=True` 模式，并原生支持 `session_id` 群聊安全过滤。

### 修复：explicit_addition 慢波合成静默丢弃与召回端断链（PR #26，Issue #25）
- 现象：美西生产副本实测，`cortex_add` 写入的事实召回 100% 返回 `NO_MATCH`，6 条 `explicit_addition` 滞留热表，被每次慢波静默丢弃。
- 慢波合成：`explicit_addition` 单独分流，`_is_authoritative` 语义不变。归属只认 `proj:` 锚点（含别名），命中不了落入 `proj:general_decisions`，不使用最近活跃实体回退，不新建陌生实体、图谱节点或边。多条事实按 `(created_at, id)` 全部并入 `metadata.facts` 并记录来源 log id；与 approval 同组时 approval 保持主结论。
- 不固化的情形：含探索或疑问措辞、或已被 `supersedes_id` 推翻的记录留在热表，并出现在慢波报告的 `skipped_explicit`（含原因）。
- 召回：`NO_MATCH` 之前新增只读末级兜底 `SCRATCHPAD_EXPLICIT_FALLBACK`（置信度 0.5，`provisional=True`，`authoritative=False`，带 `source_log_ids`）；`DECISION_TEXT_MATCH` 同时匹配 `metadata.facts` 文本，保证固化后的事实能被召回。
- 安全：`phase_hint="explicit_addition"` 此前没有任何防护，公开入口都能伪造。现在 `append_log` 拒绝，`POST /v1/scratchpad/log` 返回 `422`，MCP `baize_scratchpad_log` 返回错误。
- 冷归档 jsonl 新增 `category`、`reason` 字段，旧字段不变。
- 新增测试：`test_explicit_addition_consolidation.py`、`test_explicit_addition_recall.py`。
- 未验证：没有在生产副本复现美西现场，三条现场查询用同形态数据在本地复现。

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
