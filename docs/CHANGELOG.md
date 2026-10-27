# 变更日志

按合并时间倒序记录。每个功能 PR 合并后，补一个 `docs/*` 分支同步本文件、`README.md` 与 `docs/ARCHITECTURE.md`，同样走 PR 合并。

## 2026-10-27

### 新增：白天判定器默认只记日志（CORTEX_DAY_JUDGE）与测试数据软清理（PR #76，Closes #75）
- **背景**：白天插件的 PhaseDetector 给用户的话打 `phase_hint`，经 `POST /v1/scratchpad/log` 直接写入草稿流，夜间整理会把 approval 折叠进活体卡定案。判定器准确率没有验证过，approval / rejection 又能直接改写定案，所以先让它默认不进正式记忆。
- **开关**：服务端环境变量 `CORTEX_DAY_JUDGE`，只有等于 `apply`（忽略大小写与首尾空白）才保留白天的 approval / rejection，其他取值与未设置一律 `log_only`。开关在服务端生效，不依赖 Hermes 网关的环境变量。
- **`log_only` 的行为**：`/v1/scratchpad/log` 把 approval / rejection 降为普通行（`phase_hint` 置空），原判定写入 `reason` 列（`day_judge:approval` / `day_judge:rejection`），`noul_score` 照存，不改 schema。普通行白天兜底（最近对话层）查得到，夜间整理不处理，也能进评测样本。`apply` 下行为与此前一致。
- **candidate 照常写入（方案 A）**：approval / rejection 能直接改写定案；candidate 是隔离通道（召回带「未核实」标注，夜间由 System 2 核实，不碰 `current_decision`，不写 `decided_as` 边）。代价：核实通过的 candidate 仍会进卡片 `facts`，所以这不算严格的「不影响任何正式记忆」。
- **`/health`**：新增 `day_judge` 字段，回显当前模式，部署后可据此核对。
- **夜间整理跳过已过期的定案 / 推翻行**：`run_consolidation` 分析前先剔除 `expired_at` 非空的行。此前只有 explicit 与 candidate 分支检查 `expired_at`，approval / rejection 分支不检查，光靠软删除挡不住已打标的行。
- **软清理端点**：新增 `POST /v1/admin/expire-scratchpad`（鉴权、默认 `dry_run`）。入参 `ids`、`session_ids`、`dry_run`，至少提供一个，单次最多 200 个 id、50 个会话，超限 `422`。只设置 `expired_at` 与 `invalid_at`（与 `supersede` 同款），不改 `phase_hint`，不物理删除，重复执行幂等。响应含 `matched`、`to_expire`、`already_expired`、`ids`、`missing_ids`、`unmatched_sessions` 与 `sample`（id、会话、角色、标记、时间、前 40 字摘要）。
- **已打标的旧记录**：开关只管新写入的行，旧行不自动改动；需要清掉的用上面的端点按 id 软删除。冷归档里的历史行不动。
- **不动的路径**：`cortex_add` 与 MEMORY.md 镜像不经过判定器；MCP `baize_scratchpad_log` 的 `phase_hint` 由调用方 agent 显式传入，不属于白天判定器（有测试锁定）；夜间批量判研仍由 `CORTEX_BATCH_JUDGE` 单独控制。
- **测试**：新增 `test_issue75_day_judge_log_only`（13 例：开关取值、`/health` 回显、默认降级与 candidate 不受影响、`apply` 行为不变、降级行被最近对话召回且不被折叠、MCP 路径不受影响、插件端到端、夜间整理跳过已过期行、清理端点 dry_run / 软删除 / 幂等 / 不物理删除、过期行不再召回、入参校验与上限、鉴权）；`test_phase3_consolidation`、`test_plugin_candidate_category` 两个依赖白天打标落库的用例显式设为 `apply`；`test_phase_a_auth_coverage` 补新端点。Linux、Python 3.12 下全量 32 个测试文件通过。
- **生产执行**：先 `dry_run` 核对，再真实执行，共软删除 8 行（测试会话与定时任务文本的行），复查已无遗留。
- **部署**：只需更新并重启 baize-cortex，不用动 Hermes 网关与线上插件。合并后默认即 `log_only`；要恢复旧行为，给服务设置 `CORTEX_DAY_JUDGE=apply`。

### 修复：群聊记忆闸改为显式 chat_type + 卡片可见性（PR #73，Closes #72）
- **现象**：旧闸靠 `session_id` 含 `:group:` 或以 `oc_` 开头判断，而网关实际传入的 session_id 形如 `YYYYMMDD_HHMMSS_xxxxxxxx`，闸从未触发（网关日志里一次拦截记录都没有）。10-25 的一次群聊请求把私人活体卡、`general_decisions` 事实与未固化草稿一并注入了群对话。`/v1/recall` 各层也没有可见性概念，服务端无法区分私人卡与群可见卡。
- **规则（用户拍板：私人信息不泄露即可，其余不设限）**：
  - 只认 Hermes 传入的显式 `chat_type`，插件按 `session_id` 登记。只有 `dm` / `p2p` / `private` / `direct` 算私聊；`group`、`forum`、空值、缺失、未知值、session_id 查不到映射，一律按群聊（fail-closed），不再从 session_id 字符串猜。
  - 卡片可见性写在 `living_states.metadata_json` 的 `visibility` 键，只有显式 `group_ok` 才算群可见，缺省即私人，无需迁移。
  - 群聊下不注入：卡片 `facts` 与墓碑、草稿流（provisional / candidate / explicit 兜底）、最近对话层、PPR 排名与种子节点。`facts` 由夜间整理从任意会话折叠而来，即使卡标了群可见也无法证明每条都安全。
- **服务端**：`/v1/recall` 新增可选入参 `scope`。缺省（旧客户端）行为不变；`dm` 走全量并回显 `scope: "dm"`；其他任何值走群聊视图并回显 `scope: "group"` 与 `withheld_entity_ids`（命中但被拦截的私人卡 id，不含内容），同时写 WARNING 日志（logger `baize.recall`）。
- **客户端**：`baize/client/plugin.py` 新增 `SessionScopes`、`CortexClient.bind_session` / `recall_scoped`，`prefetch` 改走 `recall_scoped`，删除旧的 `:group:` 判断。群聊下若服务端未回显 `scope=group`（旧服务端），本轮什么都不注入；收到回显后客户端再按白名单过滤一遍（丢弃草稿与非 `group_ok` 的卡）。会话切换时仅 `compression` / `new_session` 继承父会话的 chat_type，`resume` 不继承（可能换到别的聊天）。每次拦截写日志：`chat_type` 原值、放行与拦截的数量、被拦 `entity_id`，不写卡片内容。
- **管理端点**：新增 `POST /v1/admin/living-state/visibility`（鉴权、默认 `dry_run`），只改 `metadata.visibility`，不动版本、定案与 `updated_at`。夜间整理与后续 decision 写入合并 metadata 时保留该键；`seed_initial_assets` 重新写入时同样保留已有的 `visibility`。
- **默认状态**：合并后所有现有卡缺省为私人，群聊什么都不注入。哪些卡标为群可见由用户另行决定。
- **测试**：新增 `test_issue72_group_memory_gate`（12 例：chat_type 判定矩阵、服务端群聊视图过滤、PPR 与最近对话层不外泄、网关真实 session_id 格式下的客户端矩阵、旧服务端无回显、服务端异常时的客户端二次过滤、会话切换继承、可见性端点、夜间整理与 seed 后保留、事故形态重放）；`test_prefetch_contract_and_fusion` 给 dm 用例显式登记 chat_type；`test_phase_a_auth_coverage` 补新端点。Linux、Python 3.12 下全量 31 个测试文件通过。
- **重放**：用生产库只读副本与 staged 插件重放 10-25 事故请求，群聊注入 0 行并有拦截日志，同样的问题按 dm 仍可召回。
- **部署顺序硬约束**：先部署仓库并重启 `baize-cortex.service`，后部署线上插件。新插件依赖仓库新增的 `SessionScopes`，反序会 import 失败，私聊也没有记忆；仓库先上而服务端未重启时，群聊 fail-closed 不注入，私聊不变。线上插件文件不在本仓库，随部署通道单独同步。
- **未覆盖**：MCP `baize_recall` 与 `GET /v1/living-states*` 未做 scope 控制；群聊 `sync_turn` 写入草稿流、再被批量判研或夜间整理固化的反向路径另行评估。群聊会话在生产热表里留有历史草稿行，其中处于 `judge_status='pending'` 的行在打开 `CORTEX_BATCH_JUDGE` 前需要决定如何处理。

## 2026-10-26

### Issue #64 PR 2：System 2 判研提示词定案与事实边界对齐（PR #68，Refs #64）
- 微创优化 `baize/intelligence/batch_judge.py` 中的 `_SYSTEM_PROMPT`。
- 吸收 NAS 黄金标注口径，清晰界定全局正式裁决与一次性运维指令、简单确认、状态播报、技术排查的 phase 语义边界。
- 补齐 noul 评分基线：明确陈述软件/模型客观能力或服务密钥/配置更新通知时，noul 给定 0.80~0.85 基线，解决事实候选流留存退化问题。
- 实测真模型打靶：相态准确率由 43% 提升至 72%（+29 个百分点），闲聊误入候选从 20/82 压减至 1/82，定案候选 4/4 稳固留存，事实候选成功救活恢复至 2/2（#344, #409 noul 均达 0.80）。
- 0.90 门槛下权威 FP 保持为 0，生产开关继续保持 `CORTEX_BATCH_JUDGE=off`，#64 保持开放推进。

### Issue #64 PR 1：只读批量判研评测与候选契约防护（PR #65，Refs #64）
- 修复 candidate 核实中布尔值校验漏洞，严格要求 JSON boolean，拒绝字符串 "false" / "true" 伪真值提升。
- 修复完整召回响应去重后丢「未核实」标签问题，守住 candidate·未核实 召回提示。
- 批量判研在整批写入前严格校验 noul 与 confidence 为 0~1 有限数值，非法响应进入重试并转 failed 终态，防半批脏写入；批次 ID 严格限定唯一整数。
- 新增标准库只读评测引擎 `baize.intelligence.judge_eval`（export/run/rescore），固定随机种子与双重 SHA-256 样本防篡改校验，离线重算不调模型、不改数据库。
- 生产环境开关保持 `CORTEX_BATCH_JUDGE=off`，待 NAS 侧人工黄金集复核完成后定线。

## 2026-10-25

### 修复：candidate 核实按事实类/定案类分流（PR #59，Issue #56 PR 2 / P1-3）
- **现象**：`decision_approved` 但 `confidence<0.9` 的弱信号定案进 candidate 后，夜间核实问的是「这是可固化的客观事实吗」。定案不是事实，真定案（如「好,那就这样」conf 0.85）大概率被否决并归档清退。
- **修复**：批量判入流时用现有 `category` 列记录候选类型（弱信号定案记 `decision`，高 noul 事实记 `fact`；`mark_judged` 新增可选 `category`，`COALESCE` 不覆盖已有值）。慢波核实按类型选 prompt，定案类问「是否正式定案、定的是哪件事、归属哪个实体」，通过后以 `category=decision`（及 `subject`）并入 facts。否决条目带 `reject_reason`，区分「核实为非客观事实」与「核实为非正式定案」。历史 `category` 为 NULL 的候选按 fact 处理。不新增列，不改 schema，不动 0.9 阈值。
- **红线 1 不变**：定案类核实通过也只进 facts，不改写 `current_decision`，不写 `decided_as` 边，不进墓碑。
- **验收口径修订**（审核方确认）：区分文案落在 `candidates.rejected[*].reject_reason`；`candidates.skipped` 语义保持「保留待下次核实」。
- **列复用说明**：`scratchpad_log.category` 同时被 `cortex_add`（fact/preference/rule/lesson）使用，两者靠 `phase_hint` 区分，代码处已注明。
- **测试**：TDD 先红后绿，`test_batch_judge_candidates` 新增 3 例（候选类型打标、定案类按定案问题核实且不改写定案、否决原因区分类型）。NAS 全量 **176 passed**。
- **部署**：`f5ece15` 已上美西机，`CORTEX_BATCH_JUDGE` 保持 off。
- **未完成**：P1-4 confidence 0.9 线大样本定线（美西侧准备样本中）；P2-5 插件同步，验收条件含插件写入的 candidate 必须带 `category ∈ {fact, decision}`（`/v1/scratchpad/log` 加可选字段，不改 schema）。

### 修复：批量判研切批空批 IndexError 与整步异常隔离（PR #57，Issue #56 PR 1 / P0-1 + P0-2）
- **现象（P0-1）**：`BatchJudgeRunner._slice()` 的 `current_session` 初值为 `None`，首行 `session_id IS NULL` 时 `None != None` 为假、`or` 不短路，对空列表求 `batches[-1]` 抛 `IndexError`。`fetch_pending_judgement()` 按 `ORDER BY session_id, id` 排序，SQLite 升序把 NULL 排最前，只要待判集合里出现一行 NULL 会话就必然命中。
- **现象（P0-2）**：`_batch_judge_step()` 无异常保护，`run()` 内只有 `_judge_batch` 有 try，切批与记账抛错会一路冒出 `run_consolidation()`——**整晚慢波全灭，折叠也不做**。
- **修复**：切批条件前置 `not batches`；`_batch_judge_step` 包 try，异常时先 `rollback` 未提交半批、记 WARNING（logger `baize.consolidator`），返回 `{"status": "failed", "error": ...}`（截断 200 字符），主折叠照常执行。
- **测试**：TDD 先红后绿，`test_batch_judge_candidates` 新增 2 例（两行 NULL 同批、首行 NULL 混合非 NULL 正确切批；打桩 `run` 抛错时 `run_consolidation()` 仍 `status: ok` 且定案照常折叠）。NAS 全量 **173 passed**（171 + 2）；工位 Windows 上 4 条 `recent_turns` 用例因临时 SQLite 文件占用报 `PermissionError`，`main @ 00923af` 基线同样失败，属既有平台问题。
- **部署**：`fbaedea` 已上美西机，`CORTEX_BATCH_JUDGE` 保持 off。
- **未完成**：Issue #56 PR 2（P1-3 candidate 核实按「事实类 / 定案类」分流）；开启批量判的另一前置条件——confidence 0.9 线大样本定线（≥100 条、负例 ≥30，0.85 / 0.875 / 0.9 / 0.925 四点曲线）。

## 2026-10-24

### 新增：candidate 候选流与夜间批量判研状态机（PR #54，Issue #44 PR4 / CTO 会诊红线 1+2，含美西五点反馈修订）
- **红线 1（candidate 候选流）**：高 noul 事实（`noul>=0.8` 且 exploration/chitchat）与「疑似计划」（`decision_approved` 但 `confidence<0.9`——美西反例「下周打算把机房迁到新加坡」差点变定案）统一进 `candidate`；召回以 `CANDIDATE_UNVERIFIED` 通道降权透出（置信度 0.45，排在 explicit 之后、recent_turns 之前，注入文本带「candidate·未核实」标注）；慢波由 System 2 二次核实——通过并入 `metadata.facts`（携带 noul 溯源），否决归档清退计入报告；**严禁参与定案改写**：不写 `decided_as` 边、不进墓碑、不改写 `current_decision`。
- **红线 2（批量判研状态机）**：`scratchpad_log` 增量迁移 `judge_status`/`retry_count`/`judge_source` 三列；历史双 NULL 行永不自动回填（防存量风暴）；`BatchJudgeRunner` 按会话切片 10 条/批（批不跨会话防锚定）、每条裁 300 字符、批次 ID/条数严格校验、失败重试上限 3 次入 `failed` 终态（不进任何召回层，可随 purge 清退）；approval/rejection 走 **`confidence>=0.9` 双门槛**（美西否决 noul 门槛：noul 与「是否定案」反相关，真定案 0.004~0.128，0/6 误杀）。
- **总开关 `CORTEX_BATCH_JUDGE` 默认 off**：System 2 判相态准召未经标注样本验证前不自动跑；关闭时普通行 `judge_status` 保持 NULL（与历史同语义、可被默认 purge，防关闭期 pending 无限积压）。
- **配套**：`phase_detector` 透出 `category.confidence`（缺失按 1.0 fail-open），`detect_phase_rich()` 四元组富返回、`detect_phase` 三元组签名不变（部署副本兼容）；purge 默认豁免扩展至 `pending` 行；`POST /v1/admin/backfill-judge` 一次性存量回填（必须显式时间窗，dry-run 默认，补 4 条历史漏判定案）。
- **测试**：新增 `test_batch_judge_candidates`（8 例），auth coverage 补 backfill-judge 端点（13 端点）。全量 27 套件通过。
- **部署顺序硬约束**：仓库侧先上（批量判自产 candidate）、插件侧后上（白天快速规则产 candidate）；反序会让插件写入的 candidate 行静默堆积。
- **未验证**：System 2 判相态准召（50~100 条人工标注，通过后再开开关）；confidence 0.9 线大样本定线；60 条重跑与 C 项别名回填（运维）。

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
- **验收状态（美西实弹复核后修正，2026-10-24）**：**未验收通过——代码层面收益归零**。回撤后门槛与 `302d7cc` 基线一致，「全词命中走直达层」为既有能力而非新增；#44 复现句的通过依赖别名「洋哥仓库」，而生产库该卡实测 aliases 为 `['流式卡片', '飞书流式推送']`，**不含该别名**，复现句仍落 `RECENT_TURNS_CONTEXT`。本 PR 的实际交付是：撤销噪声放大器 + 别名黑名单防污染。复现句的修复待**别名数据回填**（运维动作：为存量卡补充具体别名，如给「流式卡片」补「洋哥仓库」）。

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
