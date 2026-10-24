# 变更日志

按合并时间倒序记录。每个功能 PR 合并后，补一个 `docs/*` 分支同步本文件、`README.md` 与 `docs/ARCHITECTURE.md`，同样走 PR 合并。

## 2026-10-24

### 新增：配置改动事后沉淀通道（PR #51，Issue #44 E 项 / CTO 会诊 PR2，优先级上调 P1）
- **根因**：配置改动是「执行动作」而非「定案表述」，事前判定必然漏——#44 实测「改成北京时间凌晨3点」连关键词门都没开，本轮 4 条真实配置定案全靠人工 `cortex_add` 兜底。
- **方案**：`CortexClient.record_config_change(tool, key, before, after)`——网关在配置类工具动作**成功后**调用，沉淀为 `explicit_addition`（`category=rule`、`reason=config_change:<tool>`），走既有 `_deliver` 通道（幂等、断网入 Outbox 重放）。内容含 key、before→after 与 UTC 时间戳（仓库无 T 格式约定）；`None` 分别表达「(未设置)」/「(已移除)」。
- **脱敏在写入前完成**：key 名命中 `token|key|secret|password|passwd|credential|auth` 整值 `<REDACTED>`；值形态兜底（`sk-` 前缀、≥32 位 hex、≥40 位 base64）；值裁剪 200 字符。IP、模型名等非秘密配置保留可召回性。
- **测试**：新增 `test_phase_a_config_sediment.py`（沉淀行为五要素、密钥类与值形态脱敏零泄漏、None 标记、Outbox 重放、验收闭环——沉淀后 `recall` 立即命中 `SCRATCHPAD_EXPLICIT_FALLBACK`，无需人工 `cortex_add`）。全量 26 套件通过。
- **未验证**：真实配置改动端到端——网关接线属部署副本侧，同步时照抄本参考实现。

### 新增：SystemOne 门神 401/403 鉴权熔断（PR #49，Issue #44 PR1 代码半边）
- **现象**：OpenJev 密钥与本机 systemd 环境脱节期间，Drex `noul` 落入模糊区 (0.4, 0.6) 的每条样本白等 0.4s 收 401 再回退 Drex 答案，成本白付收益为零；且 `_try` 吞掉一切异常，失败完全静默（靠 #44 直连探针才发现）。
- **修复**：新增 `_GateBreaker` 进程内熔断——401/403（永久性凭据失效）首次命中即熔断该门神并打 `WARNING`（logger `baize.phase_detector`，未配置日志时经 lastResort 直出 stderr，失败不再静默）；熔断期内 `detect_phase` 直接跳过该门神，模糊区立即降级；TTL 600s（monotonic 时钟可注入）到期自动恢复，密钥修好无需重启进程；5xx/超时仍按临时故障处理不熔断；线程安全。
- **运维半边**：OpenJev key 本身存活（后方核账），美西机同步现役 key 属运维操作，与本 PR 互不阻塞。
- **测试**：phase7 补三例（401 熔断后请求序列只剩 OpenJev、TTL 过期恢复、5xx 双门仍全试）。全量 25 套件通过。
- **未验证**：美西机实测模糊区耗时下降，待部署后分身验证。

### 修正：召回门槛红线纠偏与别名库通用词黑名单（PR #47，Issue #44 CTO 裁决红线 3）
- **认账**：PR #45 按原单提议放宽的「单 2-gram 命中卡名/别名即放行」违反 CTO 会诊红线 3——卡名「白泽记忆」内嵌高频词「记忆」，任意一句闲聊都能把该卡以 score≈6 拽进召回。PR #45 的测试缺高频词负样本，属测试盲区。
- **门槛回撤**：`qualifies` 撤销第三析取，回到两规则（原词命中 / ≥2 个不同 2-gram）；全词命中由 `LIVING_STATE_DIRECT` 的 name/alias 子串匹配承担，长句召回靠注册**具体别名**解决。
- **别名库黑名单**：`_GENERIC_ALIAS_BLACKLIST`（记忆/方案/项目/系统/服务/配置/记录/问题/时间/数据/工具）精确匹配，过滤点在慢波 `_apply` 的别名并集处——新别名禁止入库，已入库的在该卡下次被慢波触碰时自动清洗，不误伤「时间机器」类长词。「卡片」未列入（Phase 6 既有夹具作合法别名、本域核心词），是否列入待 CTO 定夺。
- **测试**：`test_recall_single_gram_gate` 重写为红线锁定（白泽记忆反例 `NO_MATCH`、正文单 gram 仍拦、双 gram 不变、具体别名「洋哥仓库」整词命中 #44 复现句走直达层、全名命中回归保护）；`test_phase6` 补黑名单用例。全量 25 套件通过。
- **未验证**：负样本（纯闲聊）误召率不上升待美西机 60 条重跑复验。

### 修复：召回单 2-gram 命中卡名/别名即放行（PR #45，Issue #44 C 项）
- **现象**：卡片就在库里，长句查不到——`recall('你现在用得卡片是洋哥仓库最新的吗')` 落到 `RECENT_TURNS_CONTEXT`，权威记忆被末级兜底抢占。根因：无标点中文整句只切出一个原词 token，句中唯一的卡名 2-gram（「卡片」→「流式卡片」）满足不了「≥2 个 2-gram」门槛，整卡被 `DECISION_TEXT_MATCH` 刷掉。
- **修复**：`qualifies` 增加第三析取——单个 2-gram 命中 `display_name` 或 `alias` 即放行（卡名短且独特，单 gram 命中名字是强信号）；仅命中正文（decision/facts）的单 gram 仍拦，通用词噪声不放大。`LIVING_STATE_DIRECT` / PPR / 兜底路径零改动。
- **测试**：新增 `test_recall_single_gram_gate.py`（4 例：复现句命中卡名、命中别名、正文单 gram 仍 `NO_MATCH`、双 gram 正文命中不变）。全量 25 套件通过，无策略漂移。
- **未验证**：60 条真实提问重跑（权威命中率 ≥57% 且误召回不增加）待美西机 `cortex_rt_regression` 复验。

### 修复：/v1 全端点鉴权收口与代码审核遗留（PR #42，Issue #41 裁决）
- **裁决**：Issue #41 两项均选 A —— 慢波调度走本机 systemd timer（运维侧，含 `Persistent=true`、curl 超时、时区确认三处修正与月度 purge 配套建议）；`CORTEX_AGENT_TOKENS` 暂不启用，等第二个写入方。
- **鉴权覆盖**：`authenticate()` 铺满全部 `/v1/*` 端点（`/health` 除外）。此前只盖 `/v1/scratchpad/log`、`/v1/cortex/add`、`/v1/recall`，`/v1/admin/*`、`/v1/graph/*`、`/v1/living-state/decision`、`/v1/sleep/consolidate` 等破坏性端点即使配置 token 也无保护。token 未配置时行为不变（fail-open 归 `default`），当前部署零影响；切启用当天只剩配置工作。
- **勘误（Issue #41 原单）**：「回填前召回会漏掉老记忆」不成立 —— `search_recent_turns` 对 `agent_id IS NULL` 的历史行故意放行（防记忆断层），启用 token 后照常召回；回填的动机是隔离卫生（NULL 行对所有代理可见），不是召回缺失。
- **本地规则笔误**：`_EXPLORATION_KEYWORDS` 两个问号都是半角 `?`，补全角 `？`；双门神都挂降级时，全角问号结尾的疑问句不再误判 chitchat。
- **PPR 平行边**：同对节点多条边权重累加，不再互相覆盖（与单边同权重能量等价，测试锁定）。
- **慢波折叠 DELETE**：按 `_DELETE_CHUNK=500` 分块，与 `purge_scratchpad` 对齐，摆脱 SQLite 变量数上限。
- **依赖与死代码**：移除零引用的 scipy/numpy（httpx 保留并注明 TestClient 用途）；移除服务端未使用的 `app.state.detector` 初始化。
- **测试**：新增 `test_phase_a_auth_coverage.py`（12 个 `/v1/*` 端点的 401/403/正确凭据三态 + 未配置时全放行）；`test_phase7_systemone_gates` 补全角问号用例；`test_bitemporal` 补 PPR 平行边能量等价用例。全量 24 套件通过，PPR 能量分布与改前逐位一致。
- **未验证**：无生产副本回归（开发机无运行环境）；timer 尚未在美西机落地，落地后按 #41 验收标准（含 `Persistent` 补跑）复验再关单。

## 2026-10-23

### 修复：RECENT_TURNS_CONTEXT 三层收口（Issue #39）
- **现象**：美西分身对 PR #38 做验收，七条标准全过、自动注入首次打通（`messages.id=2614` 带出跨 34 小时的两条历史对话），但新层自身带出三个缺陷。老板只发一句「好，按你说的办」，注入块就被拖进三条无关历史对话——`你说的` → 2-gram `你说`/`说的` → 命中三条闲聊，其中一条是群聊旧话。
- **代理隔离**：`search_recent_turns` 原先没有 `agent_id` 条件，多代理配置 token 后 beta 代理的对话会进 default 代理的注入块。现在查询新增 `(? IS NULL OR agent_id IS NULL OR agent_id = ?)`，`agent_id IS NULL` 属于未启用 token 时期的历史行，放行以免记忆断层；`/v1/recall` 与写路径共用同一个 `authenticate(request)` 身份。
- **噪声收口**：新增 `RECENT_TURN_STOPWORDS`（纯语气/指示/连接词：可以、这个、然后、好的、你说……），过滤后若没有至少一个 ≥3 字的实义词，整层不触发——单 token 或纯 2-gram 碎片不足以「接上上一轮」。
- **零信息量行**：`RECENT_TURN_MIN_CHARS = 6`，整行只有两三个字的应答（实测库里 id=118 原文就是「可以」两个字）不再被当记忆注入。
- **排序**：从 `created_at DESC` 改为 `hits DESC, created_at DESC`（SQL 侧 `sum(CASE WHEN LIKE ...)` 计算命中 token 数），高频词命中三条行时不再由「最新」决定谁上来。
- **注入可读性**：插件侧注入行改为 `- [source · YYYY-MM-DD HH:MM] …`，34 小时前与 1 小时前的条目从此可区分。插件源码不在本仓库，改动落在部署副本，需与本仓库外维护的副本同步。
- 新增测试：`test_recent_turns_hardening.py`（7 例，覆盖代理隔离、NULL 历史行仍可召回、零信息量行、四个纯语气词、口头禅不拖旧话、实质短查询仍召回、命中数排序）。全量 `145 passed`。
- 回归探针：`cortex-memory-acceptance/scripts/cortex_rt_regression.py` 五段 19 断言，修复前基线 15/19（4 条 FAIL 精确对应本 issue），修复后复跑应为 19/19。
- 未验证：停用词表是人工枚举的 20 个词，未在长周期真实对话流上统计误杀率；`min_chars=6` 是拍的初值。

### 修复：草稿归档默认豁免待固化记录（PR #35，Issue #34）
- 现象：美西分身在生产副本上对 PR #33 做 `before_date=2026-10-22 00:00:00` 的归档演练，匹配出的 61 条里混入一条 `phase_hint='approval'` 的权威定案（id=35）。`find_for_purge` 只按 `before_id`、`role`、`before_date` 过滤，完全没有 `phase_hint` 维度，积压超过窗口的定案会被常规清理静默删除，慢波合成再也读不到。
- 修复：`find_for_purge` 默认追加 `phase_hint IS NULL`，`ScratchpadManager`、`purge_scratchpad`、`PurgeIn` 与 CLI 新增 `include_pending_phase`（CLI 为 `--include-pending-phase`），默认关闭。
- 豁免范围是所有 `phase_hint` 非空的记录，比工单列出的 `approval`、`explicit_addition` 多了 `rejection`：慢波合成读取的正是这些记录，`rejection` 是推翻旧方案的依据，删掉同样丢信息。
- 报告新增 `include_pending_phase` 与 `protected_pending`，dry-run 即可看到被保护的条数。
- 新增测试 `test_purge_protect_phase_hints.py`（6 条），其中一条验证默认归档之后 approval 仍能被下一次慢波合成固化；`test_purge_before_date.py` 中归档显式沉淀记录的用例改为显式打开开关。
- 夭夭已在生产环境实测并完成部署。

### 新增：普通对话轮次末级召回层 RECENT_TURNS_CONTEXT（Issue #37）
- **现象**：美西生产实测，老板 `/new` 开新会话后要求验收上一轮改动，分身手里没有任何上一轮上下文，只能回翻会话库。`scratchpad_log` 里当轮 user/assistant 行（id=197/198）都在，但 `/v1/recall` 对「重启开新会话了，你测吧」返回 `NO_MATCH`——**写入正常，召回结构性不通**。
- **根因**：插件 `sync_turn` 只在文本含「定案/决定/确认/批准/同意」且 PhaseDetector 判 `decision_approved` 时才写 `phase_hint`，普通轮次恒为 `NULL`；而第 4 层兜底 `SCRATCHPAD_EXPLICIT_FALLBACK` 与固化器都只查 `phase_hint` 非空（`scratchpad.py:69`/`:161`），于是普通对话只写不读。
- **修复**：`ScratchpadManager.search_recent_turns()` 绕过 `phase_hint` 语义，按 `role='user'` + `expired_at IS NULL` + 72 小时窗口 + token 命中取行，文本裁剪到 400 字符；`CognitiveRecallRouter` 在其后新增末级策略 `RECENT_TURNS_CONTEXT`（置信度 0.35，`provisional=True`，`authoritative=False`，最多 3 条），只在活体卡、PPR、决策文本、显式事实全部落空后触发。
- **否决方案**：给普通轮次写 `phase_hint='turn'`。`phase_hint IS NOT NULL` 是固化器入队条件（`export_pending_phase_logs`），会把闲聊灌进 `living_states`。
- **不变式**：显式事实（`cortex_add`）优先级高于对话层；召回严格只读，不写库、不改 `hit_count`；无 token 命中仍返回 `NO_MATCH`，不会把旧对话整段倒出来。
- 新增测试：`test_recent_turns_recall.py`（7 例）。全量 `138 passed`（原 131 + 7）。
- 未验证：未在 24 小时以上的真实对话流上做召回精度抽样；72 小时窗口与 3 条上限是拍的初值，需要用实弹数据回调。

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
