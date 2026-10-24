# 🏛️ 白泽·Cortex (Bai Ze Cortex) 架构设计白皮书

**工程标识**：`baize-cortex`  
**文档版本**：v2.1  
**审定人**：田总监 (TIAN)  
**架构执笔**：夭夭 (CTO & Chief Architect)  
**初版日期**：2026-10-20  
**最近同步**：2026-10-24（同步至 PR #54 / Issue #44 PR4：candidate 候选流与夜间批量判研状态机；含 PR #51 E 项沉淀通道、PR #49 门神熔断、PR #47/#48 红线 3 纠偏与 C 项验收勘误）  

> 每个功能 PR 合并后，本文档与 `README.md`、`docs/CHANGELOG.md` 需同步更新，并走 `docs/*` 分支 PR 合并，保证演进可追溯。

---

## 1. 认知哲学与范式跃迁

白泽 1.x 采用的“基于平铺表（Flat Table）的追加存储 + 向量检索”机制，在面对长周期多轮工程对话时，必然陷入三大致命困境：
1. **记忆污染（Memory Pollution）**：大量调试、报错、瞬态确认被视作等权事实入库，冲垮时效池；
2. **时态塌陷（Temporal Collapse）**：新旧方案在库内平权共存，检索引擎无法物理识别被推翻的陈年方案；
3. **联想僵化（Associative Rigidity）**：单步余弦相似度无法处理带有偏置或错词的跨跳工程检索。

白泽·Cortex（baize-cortex）确立了**“仿生认知双轨”**与**“双时态状态机”**的核心哲学：
- **人类记忆本质是模式补全（Pattern Completion）与重构（Reconstruction）**，不是录像带回放；
- **遗忘与睡眠修剪不是缺陷，而是系统维持抗熵增的最高算法**；
- **重大工程方案存在唯一现役真理**，必须与历史演进拓扑物理解耦。

### 1.1 架构铁律

- 存储只用 SQLite（WAL 模式，`PRAGMA foreign_keys=ON`，`synchronous=NORMAL`，`busy_timeout=30000`），零外部中间件；
- 不物理删除数据：热表清退前必须先无损写入 `cold_archive/*.jsonl.gz`；
- MCP 层为手写 JSON-RPC 2.0（协议 2024-11-05）；
- 所有外部 API 调用严格 Fail-open：网络故障绝不阻塞主系统交互；
- Schema 只做增量加法迁移，对存量数据与旧客户端零影响。

---

## 2. 四层物理正交存储模型 (Storage Topology)

底座坚持**纯嵌入式、单文件 SQLite WAL 模式**，内存预算 500MB~1GB。

### 2.1 Layer 0: 瞬态草稿流 (Episodic Scratchpad)
- **表结构**：`scratchpad_log (id, session_id, role, raw_text, phase_hint, noul_score, created_at)`，Phase A 起增补见 §2.5；
- **特性**：微秒级追加，零向量化，零阻断；
- **生命周期**：夜间慢波合成时，被折叠的记录先无损冷归档再清退；未折叠的草稿继续留在热表；
- **清洗接口**：`POST /v1/admin/purge-scratchpad`（默认 `dry_run`），真实清洗前先归档至 `cold_archive/purged_*.jsonl.gz`，再分块（每块 500）在同一事务内删除。过滤条件 `before_id`、`role`、`before_date` 为 AND 关系；`before_date` 严格小于比较，格式必须是 `YYYY-MM-DD HH:MM:SS`，带 `T` 的 ISO 格式或非法日期返回 `422`。`dry_run` 返回 `matched`、完整 `ids` 清单与最多 10 条 `sample`，不落任何删除；
- **待固化记录保护（Issue #34）**：清洗默认只处理 `phase_hint IS NULL` 的普通草稿。`approval`、`rejection`、`explicit_addition` 等带相态标记的记录是慢波合成的输入，若因积压滞留超过归档窗口被删，记忆会不可逆地丢失，因此一律豁免；不带任何过滤条件的清洗同样受保护。报告中 `protected_pending` 给出同一过滤条件下被豁免的条数，`include_pending_phase` 标明本次是否放行。只有显式传 `include_pending_phase=true`（CLI 为 `--include-pending-phase`）才会连同待固化记录一并归档，此时仍先写冷归档再删除，归档行保留 `phase_hint`；
- **批量判研状态机（Issue #44 PR4，红线 2）**：`scratchpad_log` 增量新增 `judge_status`（`pending` / `done` / `failed`）、`retry_count`、`judge_source` 三列。历史存量行为 `phase_hint` 与 `judge_status` 双 NULL，**永不自动批量判**（防存量回填风暴）；`CORTEX_BATCH_JUDGE` 开启时新写入的普通 user 行置 `pending` 进入夜间判研管线，关闭时保持 NULL（与历史同语义，可被默认 purge 清退）。判为闲聊的行 `judge_status='done'` 且 `phase_hint` 保持 NULL（`RECENT_TURNS_CONTEXT` 契约不破坏）；重试 3 次仍失败的行入 `failed` 终态，**不参与任何召回层**（`COALESCE(judge_status,'') <> 'failed'`），可随 purge 清退（有冷归档）。purge 默认豁免同步扩展至 `judge_status='pending'` 的行（未判完被删=记忆丢失）；`POST /v1/admin/backfill-judge` 提供一次性存量回填：必须显式时间窗 + 条数上限，默认 `dry_run` 出清单；
- **一键归档脚本**：`python -m baize.cortex_purge --db baize.db --days 30`，默认 dry-run，加 `--execute` 才真正归档并删除，`--days` 默认 30，`--include-pending-phase` 默认不带，数据库不存在时退出码 2。冷归档 jsonl 逐行含 `category`、`reason`，可 `gzip -d` 还原。

### 2.2 Layer 1: 项目活体状态卡 (Living State Vault)
- **表结构**：`living_states (entity_id PRIMARY KEY, display_name, version, status, current_decision, metadata_json, tombstones_json, updated_at)`；
- **特性**：一项目一文档，硬上限限制在 20~30 个核心工程实体；
- **查询复杂度**：$O(1)$ 主键直连，直接旁路所有图检索与向量比对；
- **展示名保护**：`display_name == entity_id` 视为“黑户”，慢波合成或 `POST /v1/admin/backfill-display-names` 可将其转正为自然语言名称；已有的良好名称不会被覆盖；
- **别名库**：`metadata.aliases` 增量取并集，永久保留；纯通用词（记忆/方案/项目/系统/服务/配置/记录/问题/时间/数据/工具）经黑名单**精确匹配**禁止入库，已入库的在该卡下次被慢波触碰时自动清洗，不误伤「时间机器」类长词（Issue #44 CTO 裁决，PR #47）。

### 2.3 Layer 2: 双时态实体拓扑图 (Bi-temporal Graph)
- **表结构**：
  - `graph_nodes (node_id PRIMARY KEY, label, node_type, properties_json, created_at)`
  - `graph_edges (edge_id PRIMARY KEY, src_id, dst_id, rel_type, weight, valid_at, invalid_at, created_at, expired_at)`
- **时态语义**：
  - `valid_at` / `invalid_at`：事实在现实世界中的有效时间区间；
  - `created_at` / `expired_at`：系统记录与标记该事实的时间区间；
- **切片规则**：检索时必须强制执行有效子图切片（Active Subgraph Slice），由视图 `v_active_graph_edges` 提供：
  $$\mathcal{G}_{active} = \{e \in \mathcal{E} \mid e.invalid\_at \text{ IS NULL and } e.valid\_at \le \text{datetime('now')}\}$$

### 2.4 Layer 3: 慢波合成睡眠 (Dream Consolidation)
- **机制**：离线批处理，由 `POST /v1/sleep/consolidate` 或 `python -m baize.core.consolidator` 触发。
- **流程**：
  0. 批量判研（Issue #44 PR4，`CORTEX_BATCH_JUDGE` 开启时）：`BatchJudgeRunner` 捞 `judge_status='pending'` 的 user 行，按会话切片 10 条/批（批不跨会话，缓解锚定效应）、每条裁 300 字符交 System 2 判相态，批次 ID/条数严格校验；approval/rejection 需 `confidence>=0.9`，高 noul 事实与弱信号计划进 candidate；单批失败重试上限 3 次后入 `failed` 终态，不阻塞折叠；
  1. 只读分析：扫描 `phase_hint` 非空的草稿，按实体分组，判定授权批准门；
  2. 先落盘：把将被折叠的记录写入唯一命名的 gzip 归档并 `fsync`；
  3. 单一短事务（`BEGIN IMMEDIATE`）：更新 `living_states`、写图谱节点与 `decided_as` 边、失效旧边，并只删除本次真正折叠的记录 ID；
  4. 失败整体回滚，热表数据不丢。
- **模型分工**：配置了 `CORTEX_LLM_URL/KEY` 时由 System 2 抽取 `decision / superseded / display_name / aliases`；未配置或调用失败时降级为正则规则。
- **无锚点定案兜底（Phase 8）**：授权批准且不带 `proj:` 锚点的记录，依次归入同会话最近出现的实体、30 分钟内最近更新的 ACTIVE 实体，兜底为 `proj:general_decisions`，不再滞留热表。
- **决策清洗**：定案文本去除半角 `:`、全角 `：` 与首尾空白。
- **candidate 候选核实（Issue #44 PR4，红线 1）**：`phase_hint='candidate'` 的行（白天快速规则或夜间批量判产出——`noul>=0.8` 的事实类、或 `decision_approved` 但 `confidence<0.9` 的疑似计划）由 System 2 二次核实：通过则并入对应卡 `metadata.facts`（携带 noul 溯源），否决则归档清退并计入报告 `candidates.rejected`；全程不写 `decided_as` 边、不进墓碑、不改写 `current_decision`（**严禁参与定案冲突覆盖**）。召回侧以 `CANDIDATE_UNVERIFIED` 通道降权透出（见 §3.2）。无 System 2 时候选保留待下次并计入报告，不静默丢弃。
- **显式沉淀固化（Issue #25）**：`phase_hint="explicit_addition"` 的记录（由 `cortex_add` 写入）不走授权批准门，`_is_authoritative` 语义保持不变，另由 `_is_explicit_addition` 判定准入：
  - **归属**：只认文本里的 `proj:` 锚点（含别名解析）命中已有合法实体；命中不了一律落入 `proj:general_decisions`。不使用 `_recent_entity()` 回退，不新建陌生实体、图谱节点或共现边；
  - **无损合并**：同实体多条按 `(created_at, id)` 升序全部并入 `metadata.facts`（含来源 log id、`category`、`reason`），不做“后者胜出”；与 approval 同组时 approval 保持主结论，显式事实并列补充；只有显式事实时 `current_decision` 保持原值，新建的 `proj:general_decisions` 为空字符串（该列 NOT NULL）；
  - **不固化的情形**：含探索或疑问措辞（`_EXPLORATION_RE`）、或已被 `supersedes_id` 推翻（`expired_at` 非空）的记录留在热表，并出现在慢波报告的 `skipped_explicit`（带 `id` 与原因），不静默丢弃；
  - **模型边界**：LLM 路径只看到用户 approval 等日志，看不到显式事实；事实无论 LLM 是否可用都写入卡片；
  - **幂等与原子性**：卡片写入与日志清退仍在同一个 `BEGIN IMMEDIATE` 事务内，失败整体回滚，重复运行不产生重复条目；冷归档 jsonl 额外保存 `category`、`reason`。

### 2.5 Phase A 增量列（三表共有）

`scratchpad_log`、`living_states`、`graph_edges` 均新增以下可空列，迁移由 `baize/storage/migrations.py` 在 `init_db` 中自动执行，可重复执行，迁移前备份数据库文件，存量回填分批提交：

| 列 | 类型 | 说明 |
|---|---|---|
| `idempotency_key` | TEXT | 幂等键，局部唯一索引 `idx_<表>_idempotency`（仅非空时生效） |
| `valid_at` / `invalid_at` | TEXT | 事实现实生效 / 失效时点 |
| `ingested_at` / `expired_at` | TEXT | 认知入库 / 认知废弃时点 |
| `supersedes_id` | INTEGER | 被本记录推翻的旧记录 ID |
| `pinned` / `hit_count` / `last_recalled_at` | INTEGER / INTEGER / TEXT | Phase A 只建列；`hit_count`、`last_recalled_at` 自 Issue #32 起由召回路由对 `living_states` 回写，`pinned` 仍为预留 |
| `agent_id` | TEXT | 写入方归属，由服务端凭据确定 |
| `judge_status` / `retry_count` / `judge_source` | TEXT / INTEGER / TEXT | Issue #44 PR4 批量判研状态机，语义见 §2.1 |

另有 `scratchpad_log.category`、`scratchpad_log.reason`，保存 `cortex_add` 的分类与原因。`graph_edges` 已存在的 `valid_at / invalid_at / expired_at` 保持原样，迁移自动跳过。

**时间格式约定**：所有时间戳统一为 `YYYY-MM-DD HH:MM:SS`（UTC），与 SQLite `CURRENT_TIMESTAMP` 兼容。带 `T` 的 ISO 格式会破坏视图中 `valid_at <= datetime('now')` 的字符串比较，禁止使用。

**回填来源**：`scratchpad_log` 与 `graph_edges` 用 `created_at`，`living_states` 没有该列，用 `updated_at`。

---

## 3. 检索与计算引擎

### 3.1 有效子图上的 PPR 能量扩散

为解决单步向量检索面对模糊词或偏置词抓瞎的问题，白泽 2.0 引入基于 Personalized PageRank 的吸引子滑落机制：

1. **种子能量注入**：将用户输入的实体词或关键词映射为拓扑图中的种子节点集 $S$，分配初始能量向量 $\mathbf{p}$；
2. **带阻尼的概率游走**：
   $$\mathbf{r}^{(k+1)} = (1 - \alpha) \mathbf{P}^T \mathbf{r}^{(k)} + \alpha \mathbf{p}$$
   其中 $\mathbf{P}$ 为有效子图 $\mathcal{G}_{active}$ 的转移概率矩阵，$\alpha$ 为重启阻尼因子（取 0.15）；
3. **物理隔离旧方案**：已推翻方案的边在 $\mathcal{G}_{active}$ 中已被切断，能量无法向失效节点扩散，能量自发汇聚到连接最密集的现役核心定案。

### 3.2 统一认知召回路由 (Phase 4 / Phase 6)

`CognitiveRecallRouter.recall(query, top_k)` 按以下顺序依次尝试，命中即返回：

| 顺序 | 路由 | 触发 | 说明 |
|---|---|---|---|
| 1 | `LIVING_STATE_DIRECT` | 查询命中活体卡的 `display_name` 或 `aliases` | 名称出现在原查询或剥离语气助词后的查询中即命中；反向包含（剥离后的查询是名称的子串）仅在名称长度不少于 4 时生效。可能命中多张卡，按 §3.3 打分排序后取前 `top_k`，`state` 为第一名。置信度 1.0 |
| 2 | `PPR_SPREADING` | 图谱节点 `label` 出现在查询中，得到种子节点 | 在有效子图上做 PPR 扩散，返回能量排序与 `resolved_state` |
| 3 | `DECISION_TEXT_MATCH` | 没有任何种子节点时的兜底 | 用「原词 + 中文 2-gram」双通道分词，对全部现役卡打分，达到命中门槛的按 §3.3 排序后取前 `top_k`，不再首个命中即返回；命中时同步检索显式事实并作为 `provisional_facts` 融合返回（Issue #28），第 4 层的检索并非只在本层落空才执行。置信度 0.6 |
| 4 | `SCRATCHPAD_EXPLICIT_FALLBACK` | 以上都未命中 | 末级只读兜底：检索 `scratchpad_log` 中 `phase_hint='explicit_addition'` 且 `expired_at IS NULL` 的未固化记录，复用分词、转义 LIKE 通配符，按 `created_at` 倒序取 `top_k`。置信度 0.5，响应带 `provisional=True`、`authoritative=False`、`source_log_ids`、`facts` |
| 5 | `CANDIDATE_UNVERIFIED` | explicit 未命中，且库内有 token 命中的 candidate 候选行（Issue #44 PR4，红线 1） | 未核实候选的降权透出：检索 `phase_hint='candidate'` 且 `expired_at IS NULL` 的行，按命中 token 数倒序取 `min(top_k, 3)` 条；置信度 0.45，`provisional=True`、`unverified=True`，插件注入文本带「candidate·未核实」标注；慢波核实通过后改走活体卡，被否决的行已清退不再进本层 |
| 6 | `RECENT_TURNS_CONTEXT` | 以上都未命中，且窗口内有 token 命中的普通对话轮（Issue #37 / #39） | 会话短期连续性兜底：检索 `scratchpad_log` 中 `role='user'`、`phase_hint IS NULL`、`expired_at IS NULL`、`COALESCE(judge_status,'') <> 'failed'`（Issue #44：failed 毒丸不进任何召回层）、`created_at >= now - 72h`、`length(trim(raw_text)) >= 6`，且 `agent_id` 为空（未启用 token 时期的历史行）或与当前代理一致的行；查询侧先滤掉纯语气/指示/连接词（`RECENT_TURN_STOPWORDS`），并要求至少一个 ≥3 字的实义词，否则整层不触发。结果按命中 token 数倒序、再按 `created_at` 倒序取 `min(top_k, 3)` 条，每条文本裁剪到 400 字符。置信度 0.35，`provisional=True`、`authoritative=False`，带 `source_log_ids`、`facts`；严格只读 |
| 7 | `NO_MATCH` | 全部未命中 | 返回空结果，置信度 0.0 |

兜底的约束：Layer 1 活体卡始终是唯一权威，草稿事实只作为未固化窗口期的明确标注回退；草稿兜底纯只读，不进入 PPR 扩散、不修改 `scratchpad_log`；活体卡直达命中时绝不触发兜底；慢波固化之后，同一事实改由活体卡策略命中。

**为什么需要 RECENT_TURNS_CONTEXT 层（Issue #37）**：网关插件 `sync_turn` 每轮都写 Layer 0，但 `phase_hint` 只在 PhaseDetector 判 `decision_approved` 时才非空，而第 4 层与固化器都只认 `phase_hint IS NOT NULL`，于是普通对话永久「只写不读」：新会话开场拿不到上一轮的任务上下文，只能回翻原始会话库。第 5 层绕开 `phase_hint` 语义，按 `role` + 时间窗 + token 命中取行，只服务跨会话短期连续性，因此窗口短（72h）、条数少（≤3）、文本裁剪（400 字符）、置信度低（0.35），不会与权威记忆争夺排序。**否决的替代方案**：给普通轮次写 `phase_hint='turn'`——`phase_hint IS NOT NULL` 是固化器入队条件（`export_pending_phase_logs`），会把闲聊灌进 `living_states`。

### 3.3 活体卡打分与时效排序（Issue #32）

**分词**：`_scoring_tokens` 先剥离语气助词，再产出「原词 + 中文 2-gram 滑窗」，去重保序。例如 `飞书卡片` → `飞书卡片 / 飞书 / 书卡 / 卡片`，不需要用户手动加空格；英文与数字只保留原词。

**打分**：对每张现役卡计算

$$\text{Score} = 3\,[\text{display\_name 命中}] + 2\,[\text{alias 命中}] + 1\,[\text{current\_decision 或 facts 命中}] + 2\,N_{\text{hit}} + \max(0,\ 1 - 0.05 \times \text{days\_since\_update})$$

方括号为「任一 token 命中即记 1」，$N_{\text{hit}}$ 为命中的不同 token 个数，匹配不区分大小写。时效因子在更新 20 天后衰减到 0，不会为负。

**命中门槛（`DECISION_TEXT_MATCH`）**：至少一个原词命中，或至少两个不同的 2-gram 命中。单个 2-gram 的偶然巧合（如「方案」「飞书」中的某一对字）不足以让一张卡入选，避免通用词制造噪声。**名称 2-gram 碎片严禁放行**（Issue #44 CTO 裁决红线 3，PR #47 纠偏）：「记忆」⊂「白泽记忆」这类高频词碎片会把纯闲聊误召成卡片——全词命中由 `LIVING_STATE_DIRECT` 的 name/alias 子串匹配承担，长句召回靠注册具体别名解决（通用词别名经黑名单清洗，见 §2.2）。**注意：机制落地不等于复现句已修复**——别名数据属生产侧内容，存量卡需运维补充具体别名后才生效（Issue #44 美西复核：C 项生产收益当前为 0）。

**排序**：Score 降序；同分取 `updated_at` 新的；再同分按 `entity_id` 升序，结果确定。输出严格遵守调用方 `top_k`（小于 1 按 1 处理），每条 `living_states` 带 `score`。

**时效回写**：被返回的活体卡执行 `hit_count += 1` 与 `last_recalled_at = 当前 UTC`，不改动 `updated_at`；未命中和草稿兜底不回写。回写失败（如被锁或被触发器拒绝）时回滚并忽略，召回照常返回（Fail-open）。PPR 策略中只对最终解析出的那张卡回写。

---

## 4. 双系统协同 (Dual-System Governance)

### 4.1 System 1：SystemOne 双门神（Phase 7）

- **职责**：会话入口意图分类、瞬时相变嗅探（`approval` / `rejection` / `exploration`）；
- **协议**：向门神 POST `{model, state, questions: {should_memorize: {type: noul, ...}, category: {type: choice, criteria: {...}}}}`，请求头带 `User-Agent: Baize/Gate-v2.0 (TypeSafe-Client)`；
- **仲裁**：先问 Drex-Max；其 `noul` 得分落在模糊区间 (0.4, 0.6) 时，再交 OpenJev 复判；
- **鉴权熔断（Issue #44 PR1，PR #49）**：门神返回 401/403（永久性凭据失效）时进程内熔断该门神（`_GateBreaker`，TTL 600s），熔断期内直接跳过不再白等，到期自动恢复；熔断打 `WARNING`（logger `baize.phase_detector`），失败不再静默。5xx/超时属临时故障，不熔断；
- **Fail-open**：两道门都不可用时，降级为本地关键词规则（`_detect_local`），不阻塞写入；
- **返回**：`detect_phase(text) -> (phase, confidence, category)`。

### 4.2 System 2：深度推理模型

- **职责**：夜间慢波合成中的抽象提炼与矛盾仲裁，通过 `OpenAICompatClient` 调用兼容 OpenAI 协议的网关；
- **边界**：DeepSeek 等推理模型只负责慢波折叠，不参与白天的 System 1 判定。

---

## 5. 承重架构加固与生产可靠性 (Phase 5 Hardening)

经过实弹穿透式红蓝军对抗审计（F1~F9），系统补齐了关键工程承重墙，兑现工业级可追责与无损保证：

### 5.1 授权批准门与防误盖章 (F2)
- **硬隔离**：`_is_authoritative(log)` 严格判定。只有 `role == "user"` 且显式标记 `phase_hint == "approval"`（且剔除探讨、反问与假设句）的记录才具备改写 `living_states` 正式定案的公章权限；
- **防纂改**：Assistant 的方案建议、假设性推演（`如果...` / `探讨` / `?` / `呢$`）一律只能沉淀为草稿或候选，严禁隐式盖章。

### 5.2 无损冷归档与 Write-Ahead 安全清退 (F1)
- **唯一性防覆盖**：归档文件命名为 `{前缀}_{毫秒时间戳}.jsonl.gz`（慢波合成前缀 `scratchpad_{日期或all}`，清洗前缀 `purged`），以排他模式创建；
- **先落盘后清退 (Write-Ahead)**：只有物理写入 gzip 并落盘校验后，才执行 `DELETE FROM scratchpad_log WHERE id IN (...)`；
- **未折叠草稿保留**：清退范围仅限**本次真正被成功折叠入活体卡的日志 ID**，未进入折叠的瞬态草稿继续留在热表等待下一轮处理，彻底杜绝数据丢失。

### 5.3 严格时态切片与图文双写同步 (F4 / F5)
- **有效子图双时态约束**：未生效的未来边（如预定生效的提案）被物理隔离在现役子图外；
- **图文双写与别名保护**：慢波睡眠折叠出新定案后，自动为项目建立 `decided_as` 现役新边，并按 `node_id` 失效旧边；`update_decision` 强制增量合并 `metadata`，永久保留 `aliases` 别名库。

---

## 6. Hermes Agent 插件与调用契约规范

### 6.1 MemoryProvider 基类契约对齐
Hermes 网关核心的 `sync_turn` 采用 `(user_content, assistant_content, *, session_id, ...)` 签名调用。`plugins/cortex/__init__.py` 必须严格拆解：
- 用户消息以 `role="user"` 写入，并由 PhaseDetector 动态嗅探打标 `approval` 或 `rejection`；
- 助手消息以 `role="assistant"` 写入，`phase_hint=None`，杜绝角色错位与死锁。

### 6.2 环境变量

| 变量 | 作用 | 未配置时 |
|---|---|---|
| `CORTEX_LLM_URL` / `CORTEX_LLM_KEY` | System 2 网关地址与令牌（需同时配置） | 慢波合成降级为正则规则 |
| `CORTEX_REASONING_MODEL` | 慢波合成使用的模型 | 默认 `cbcn/deepseek-v4.1-flash` |
| `CORTEX_SYSTEMONE_DREX_URL` / `_KEY` | Drex-Max 门神 | 跳过该门 |
| `CORTEX_SYSTEMONE_OPENJEV_URL` / `_KEY` | OpenJev 门神 | 跳过该门 |
| `CORTEX_SYSTEMONE_PROXY` | 门神请求使用的代理 | 直连 |
| `CORTEX_AGENT_TOKENS` | 凭据表，格式 `token:agent_id,token2:agent_id2` | 不校验，所有写入归属 `default` |
| `CORTEX_BATCH_JUDGE` | 夜间批量判研总开关（Issue #44 PR4）；`1`/`true`/`on` 开启 | 不跑批量判，普通行 `judge_status` 保持 NULL（与历史存量同语义）；**System 2 判相态准召经 50~100 条标注样本验证通过后才允许开启** |

> 历史变量 `CORTEX_DECISION_MODEL` 自 Phase 7 起已不再使用，被 SystemOne 双门神变量取代。

**`CORTEX_AGENT_TOKENS` 启用决策（Issue #32，总监裁定：暂缓启用）**：服务仅绑定 `127.0.0.1:8768`，当前唯一写入方是 Hermes 网关，属于单机回环，不是漏洞敞口而是隔离能力未启用；此时配置只会多出两端需同步维护的 token（轮换与排障成本翻倍），收益拿不到。不存在“只记归属但不强制鉴权”的中间档，该变量非空即强制校验。

满足任一条件时必须切换为启用：
1. 接入第二个写入方（NAS 实例、第二分身或其他服务）；
2. 需要按 agent 回溯写入来源；
3. Cortex 对本机以外的地址可达，哪怕仅在局域网内。

启用时服务端设 `CORTEX_AGENT_TOKENS=<token>:<agent_id>`，插件端设 `CORTEX_TOKEN=<token>`，两端必须同时改，且先重启 Cortex 再重启网关，反向会在窗口期产生 `401`，插件 Fail-open 静默入 Outbox，不易察觉。历史 `agent_id IS NULL` 的行需按 `session_id` 回填——注意回填的动机是**隔离卫生**（NULL 行对所有代理可见），而非召回缺失：`search_recent_turns` 对 NULL 行故意放行以防记忆断层（§3.2 第 5 层），启用 token 后老记忆照常召回。token 轮换写入运维手册。

**鉴权覆盖面（PR #42）**：全部 `/v1/*` 端点统一经 `authenticate()` 凭据校验（`/health` 除外），`/v1/admin/*`、`/v1/graph/*`、`/v1/living-state/decision`、`/v1/sleep/consolidate` 等破坏性端点不再游离于凭据体系之外；未配置 `CORTEX_AGENT_TOKENS` 时全端点放行并归属 `default`，行为与既往一致。切启用当天只剩配置工作，无需再改代码。

---

## 7. 记忆外溢通道与可靠投递 (Phase A)

### 7.1 背景

Hermes 内置 `memory` 工具写入 `MEMORY.md` 有 2200 字符上限，超限时返回 `2426/2200` 这类含动态数字的报错。Agent 循环缺少同类错误熔断，字面比对无法识别为同一错误，曾出现连续 80+ 次重试、单轮耗时超 120 秒。同时客户端没有向 Cortex 主动写入的通道。

### 7.2 服务端写入接口

| 接口 | 说明 |
|---|---|
| `POST /v1/cortex/add` | 显式沉淀：`content`（≤2000 字符）、`category ∈ {fact, preference, rule, lesson}`、`reason`（≤500 字符）、可选 `supersedes_id` |
| `POST /v1/scratchpad/log` | 草稿追加；携带 `idempotency_key` 时同样走幂等与长度校验 |

约束：
- **凭据**：配置 `CORTEX_AGENT_TOKENS` 后要求 `Authorization: Bearer <token>`，服务端据此确定 `agent_id`，忽略请求体里伪造的 `source`、信任等级、`agent_id` 字段；缺凭据 `401`，凭据无效 `403`；
- **幂等**：`idempotency_key` 命中已有记录返回 `200`，响应含已存 `id` 与 `deduplicated: true`；若该键已被其他 `agent_id` 占用返回 `409`；
- **状态码语义**：`413` 内容超长，`422` 内容不可接受，`401/403` 凭据问题，`409` 幂等键被其他 agent 占用，`5xx` 服务端临时故障。服务端当前不主动返回 `429`；客户端 Outbox 已按 `429` + `Retry-After` 实现遵守逻辑，供前置网关限流时使用；
- **`explicit_addition` 标记保护**：`phase_hint="explicit_addition"` 只能由 `cortex_add` 内部路径写入。`ScratchpadManager.append_log` 直接拒绝该值，`POST /v1/scratchpad/log` 返回 `422`，MCP `baize_scratchpad_log` 返回错误，防止客户端伪造显式沉淀；
- **`supersedes_id`**：同一事务内校验“目标存在、非自身、同 `agent_id`”，通过后将旧记录的 `expired_at` 置为当前时间、`invalid_at` 置为新记录的 `valid_at`，并写入一条 `superseded_by` 图谱边。不带 `supersedes_id` 时不凭空造边。

### 7.3 规范化与幂等键

`baize/core/normalization.py` 为客户端与服务端共用实现：
- 规范化：NFKC → `strip` → 连续空白折叠为单个空格；
- 幂等键：`sha256(session_id + "\x00" + normalized_content).hexdigest()[:32]`（128 位）；
- 契约夹具：`tests/fixtures/normalization_vectors.json`（golden vectors），两端由同一份夹具锁定一致。

### 7.4 客户端参考实现 `baize/client/`（纯标准库）

| 模块 | 职责 |
|---|---|
| `outbox.py` | `~/.hermes/outbox.jsonl`，每行一条信封（`idempotency_key / payload / created_at / attempts / next_retry_at`）。追加使用 `O_APPEND` 单次 `os.write`；flush 时在锁内原子 rename 为 `outbox.sending.<时间戳>` 再逐行投递；崩溃后重放遗留的 `sending` 文件。跨进程锁：POSIX 用 `fcntl.flock`，Windows 用 `msvcrt.locking` |
| `plugin.py` | `CortexClient.cortex_add(content, category, reason, supersedes_id=None)`：3 秒超时或网络异常、`5xx`、`429` 时不报错，自动入队并返回 `{"status": "queued", ...}`；`record_config_change(tool, key, before, after)`（Issue #44 E 项）：配置类工具动作**成功后**事后沉淀 `explicit_addition`（`category=rule`、`reason=config_change:<tool>`、内容含 before→after 与 UTC 时间戳），密钥类 key 名整值掩蔽、`sk-`/长 hex/长 base64 值形态兜底掩蔽，脱敏在写入前完成；`on_memory_write(action, target, content, metadata)`：`MEMORY.md` / `USER.md` 写入后把单条条目入队，标记 `origin="hermes_builtin_mirror"`，遇到 `origin="writeback"` 直接跳过以切断回环，钩子内部异常一律吞掉 |
| `breaker.py` | 错误签名归一化与熔断 |

**Outbox 错误分类**：
- `2xx`（含去重命中）：ACK；
- `400 / 413 / 422`：进入 `outbox.dead.jsonl` 死信并记 WARNING；
- `401 / 403`：不丢弃，暂停本轮投递并按退避重试；
- `429`：遵守 `Retry-After`；`5xx` 与超时：指数退避加抖动；
- 后台线程 `daemon=True`，捕获所有内部异常，不向主会话抛出。

### 7.5 错误签名归一化与熔断

签名生成按顺序做正则替换：UUID → `<UUID>`，ISO 时间戳与 epoch → `<TS>`，文件路径 → `<PATH>`，长度 ≥ 8 且含字母的十六进制串 → `<HEX>`，其余数字（含 `2426/2200`）→ `<N>`；最终签名为 `tool_name | error_class | 归一化模板`。

熔断规则：
- 同一会话、同一工具、同一签名连续失败 2 次立即触发；
- 触发后短路后续调用，合成返回：“MEMORY.md 空间不足，本轮禁止继续重试 memory 工具。请改用 cortex_add 工具将内容沉淀至白泽 Cortex 长期库。”
- 一次成功调用或新用户回合开始（`new_turn`）时复位。

### 7.6 插件端预取契约与记忆透出（Issue #27 / Issue #30）

- **Hermes MemoryProvider 契约对齐**：Hermes 核心调度层 `agent/turn_context.py` 与 `MemoryManager` 要求 `prefetch(self, query: str, *, session_id: str = "") -> str`。若返回非字符串（如 `List`），消费端执行 `.strip()` 会抛出 `AttributeError` 并被调度层 `suppress(Exception)` 静默吞噬。因此插件端将结构化提取出的记忆格式化为换行分隔自然文本（`- [source] [display_name] content`），空召回返回 `""`；
- **状态指示器透明化**：实现 `recall_status() -> Optional[RecallStatus]`，在上轮命中记忆条数 $>0$ 时返回 `RecallStatus(provider_label="baize-cortex", count=N, glyph="🧠")`，驱动前端渲染召回提示；
- **群聊安全闸门（Fail-closed）**：对含 `:group:` 或以 `oc_` 开头的群聊会话，`prefetch` 恒返回 `""`，严格阻断私域记忆向群聊外溢。

### 7.7 部署与归属

插件代码在本仓库 `baize/client/` 完成自闭环测试后，由 NAS / 云机部署通道同步至 Hermes 的 `plugins/cortex/`。构建、容器重启与数据库迁移的实际执行由 NAS 侧（夭夭）负责。

---

## 8. REST 与 MCP 接口总览

### 8.1 REST（`baize/server/app.py`）

| 路径 | 方法 | 说明 |
|---|---|---|
| `/health` | GET | 健康检查 |
| `/v1/scratchpad/log` | POST | 草稿流追加（支持幂等） |
| `/v1/cortex/add` | POST | 显式沉淀（幂等、可推翻旧记录） |
| `/v1/living-states` | GET | 列出现役活体卡 |
| `/v1/living-state/{entity_id}` | GET | 读取单张活体卡 |
| `/v1/living-state/decision` | POST | 就地更新定案并写墓碑 |
| `/v1/graph/node` · `/v1/graph/edge` · `/v1/graph/invalidate` | POST | 图谱节点、边与失效 |
| `/v1/graph/active-subgraph` | GET | 现役有效子图 |
| `/v1/recall/ppr` | POST | PPR 能量扩散 |
| `/v1/recall` | POST | 统一认知召回 |
| `/v1/sleep/consolidate` | POST | 触发慢波合成（`dry_run` 仅返回空报告） |
| `/v1/admin/backfill-display-names` | POST | 黑户展示名转正，默认 `dry_run` |
| `/v1/admin/purge-scratchpad` | POST | 草稿清洗，默认 `dry_run`，先归档再删除；过滤参数 `before_id`、`role`、`before_date`（`YYYY-MM-DD HH:MM:SS`）为 AND 关系；默认豁免 `phase_hint` 非空的待固化记录，`include_pending_phase=true` 才放行 |
| `/v1/admin/backfill-judge` | POST | 一次性存量判研回填（Issue #44 PR4）：必须显式 `after_date` / `before_date` 时间窗 + 条数上限，默认 `dry_run` 出清单，防止全量存量回填风暴 |
| `/v1/admin/seed` | POST | 预置核心工程资产 |

全部 `/v1/*` 端点经 `authenticate()` 凭据校验（`/health` 除外）：未配置 `CORTEX_AGENT_TOKENS` 时放行并归属 `default`；配置后缺失凭据 `401`、凭据无效 `403`。PR #42 起覆盖全部端点，此前仅 `/v1/scratchpad/log`、`/v1/cortex/add`、`/v1/recall` 受保护。

### 8.2 MCP stdio 工具（`baize/server/mcp_server.py`）

`baize_get_living_state`、`baize_list_active_states`、`baize_update_decision`、`baize_scratchpad_log`、`baize_recall_ppr`、`baize_consolidate_sleep`、`baize_recall`。

---

## 9. 验证套件

在仓库根目录以 Python 3.12 运行，需设置 `PYTHONPATH=.`，每个文件均可 `python tests/xxx.py` 直接执行。当前共 27 个套件，其中 Issue #34 新增 `test_purge_protect_phase_hints`（草稿归档默认豁免待固化记录），Issue #37 新增 `test_recent_turns_recall`（普通对话轮召回、assistant 行排除、时间窗、显式事实优先、只读、文本裁剪、未命中仍 `NO_MATCH`），Issue #39 新增 `test_recent_turns_hardening`（代理隔离、NULL 历史行、零信息量行、停用词过滤、命中数排序），Issue #32 新增 `test_recall_scoring`（分词、打分、Top-K、时效排序与命中回写）与 `test_purge_before_date`（时间窗归档与一键脚本），Issue #44 E 项新增 `test_phase_a_config_sediment`（沉淀五要素、脱敏零泄漏、None 标记、Outbox 重放、召回闭环），Issue #44 PR4 新增 `test_batch_judge_candidates`（状态机写入语义、confidence/noul 双门槛分流、切片与批次校验、重试 failed 终态、candidate 核实折叠与禁改写、开关三态、召回通道排序与未核实标注、purge 保护与回填窗口强制），Issue #44 红线 3 落地为 `test_recall_single_gram_gate`（高频词嵌卡名不放行、正文单 gram 仍拦、双 gram 不变、全词别名/全名命中走直达层）；PR #49 在 `test_phase7_systemone_gates` 补鉴权熔断用例（401 熔断后跳过重试、TTL 到期恢复、5xx 不熔断）。PR #42 新增 `test_phase_a_auth_coverage`（12 个 `/v1/*` 端点的 401/403/正确凭据三态与未配置时全放行），并在 `test_phase7_systemone_gates` 补全角问号本地规则用例、`test_bitemporal` 补 PPR 平行边能量等价用例。其余为：`test_bitemporal`、`test_phase1_mvp` 至 `test_phase8_consolidation_closure`，Phase A 的八个 `test_phase_a_*`（迁移 / Outbox / cortex_add / 规范化 / 镜像 / 熔断 / 鉴权覆盖 / 配置沉淀），Issue #25 的 `test_explicit_addition_consolidation`（慢波固化）与 `test_explicit_addition_recall`（召回可见性），以及 Issue #27 的 `test_prefetch_contract_and_fusion`（`living_states` 契约与未固化事实融合）。
