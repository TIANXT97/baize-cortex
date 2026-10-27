# 变更日志

按合并时间倒序记录。每个功能 PR 合并后，补一个 `docs/*` 分支同步本文件、`README.md` 与 `docs/ARCHITECTURE.md`，同样走 PR 合并。

## 2026-10-27

### 文档：补记 #79、#80、#82 与漏记的变更日志，补 #78 架构说明（Closes #91）
- **背景**：上一轮文档同步（#89）写明 #78 / #79 在变更日志与架构说明里还没有专门条目。借这次补档，把全部已合并 PR 的编号和变更日志对了一遍，又发现几处没有条目或没写 PR 号的改动。目的是让每个合并过的改动为什么做、怎么做的都能查到。
- **做法**：只改文档。CHANGELOG 新增 #79（Closes #78）、#80、#82（Closes #81）三条；补上漏记的 #28（Closes #27）、#61（Closes #56）、#71、#24 四条；给已有条目补 PR 号：#38、#40、#53。ARCHITECTURE 新增 #78 的机制说明：§2.2 补自动墓碑、与 `decided_as` 的关系、回滚；新增 §2.6 卡片历史表；§2.4 补 `dry_run` 预览的 `changes` 字段；§8.1 更新定案接口与回滚接口的说明；§9 补测试说明。
- **核对**：表名、接口路径、参数名、字段名、来源取值都对照代码与 PR 的实际 diff 核对过。
- **对照结果**：下列纯文档同步 PR 没有单独补条目，因为它们只是为已有条目补文档，内容已经体现在对应的功能条目里：#29（#26）、#36（#35）、#43（#42）、#46（#45）、#48（#47）、#50（#49）、#52（#51）、#55（#54）、#58（#57）、#60（#59）、#66（#65）、#69（#68）、#74（#73）、#77（#76）、#90（#85 #86 #88）。其中 #24 不只是同步（新建了变更日志、升级了白皮书、写入了项目约定），单独记了一条；#53 更正了 #47 的验收状态，这条更正已经写在 #47 的条目里，这次只补上 PR 号。
- **未覆盖**：条目按当时的 PR 正文与代码整理，没有重新做线上验证；#80 之前的提交与提交信息不改写，见 #80 条目。

### 文档：同步 #85 #86 #88 的变更日志、README 与架构说明（Closes #89）
- **背景**：#85、#86、#88 合并并上线后，变更日志、README 和架构说明还没有跟上，只能靠读代码才知道新增了哪些开关、字段和行为。
- **做法**：只改文档。CHANGELOG 补下面三条，每条链上对应的 PR 与 issue；README 补路线图、目录骨架、整理的忙响应与 `skipped_changed`、测试文件数量；ARCHITECTURE 补 §2.4 整理三段式与互斥、§3.2 方案节点追溯、§6.2 环境变量表、§8.1 接口表、§8.2 MCP 说明与 §9 验证套件。
- **核对**：每个开关名、字段名、默认值、取值都逐个对照了代码；测试文件数与鉴权覆盖项数以仓库当前内容为准（36 个测试文件、鉴权覆盖 17 项）。
- **顺带**：§8.1 接口表里 `/v1/sleep/consolidate` 的说明仍写着 `dry_run` 只返回空报告，已改为实际行为；表里补上已存在的卡片历史查询与回滚两个接口。
- **未覆盖**：#78（墓碑、卡片历史与回滚）本身在 CHANGELOG 与架构说明里还没有专门条目，本次没有补。

### 修复：带已作废方案原话的问题顺着方案节点追溯现役卡，图检索无结果时继续走兜底层（PR #86，Closes #84）
- **现象**：问题里带方案原话（旧方案或现行方案）时，召回返回空结果。夜间整理为每个定案建一个方案节点（标签就是定案文本）并给卡片连一条 `decided_as` 边，旧方案被取代后这条边失效；召回用节点标签选 PPR 种子，会命中方案节点，但方案节点不是活体卡，PPR 又只在现役子图上跑，`resolved_state` 为空；PPR 分支无论是否解析出卡片都直接返回，后面的显式事实、candidate、最近对话兜底全部被挡掉。
- **做法**：PPR 解析出现役卡时行为不变。解析不出时，`_plan_trace` 顺着 `decided_as` 边（含已失效的）从方案节点找到所属的现役卡，返回 `strategy: PLAN_NODE_TRACE`、置信度 0.8、卡片的现行定案；`superseded_plan_matched` 为 `true` 表示有卡只能通过已失效的边追溯到，即问题里命中的是已被取代的旧方案。同时合并按文本命中的其它卡与未固化的显式事实。只认 `decided_as` 关系，只返回 `ACTIVE` 的卡，作废节点自己的内容从不当作现行结论返回。追溯不到任何卡时不再直接返回，继续走抽成 `_fallback_chain` 的原有兜底链：文本命中、显式事实、candidate、最近对话、`NO_MATCH`，顺序与内容不变。
- **群聊视图**：沿用 #72 的闸。追溯到的卡只有 `group_ok` 才返回（白名单字段，不带显式事实与 `seed_nodes`）；追溯到私人卡只把 `entity_id` 记入 `withheld_entity_ids` 并写 WARNING 日志，不返回任何内容；一张可见卡都追溯不到时，按群聊视图原有的文本命中规则继续。
- **开关**：无新开关。
- **测试**：新增 `test_issue84_plan_node_trace`（6 例，假数据）：旧方案原话追溯到现行方案并带出未固化事实；现行方案原话同样追溯到所属卡；图检索解析不出时继续走兜底层（显式事实或 `NO_MATCH`）；直达与 PPR 命中不受影响；群聊请求不返回私人卡，标成群可见后可追溯但不带 facts 与草稿；只认 `decided_as` 与现役卡。生产库只读副本上用 24 条查询对比：完全空结果由 8 条降到 0 条，现行方案的 3 条查询由全空变为全部追溯到所属卡；群聊视图下返回的非群可见卡与草稿类条目修前修后都是 0。
- **部署**：只改服务端，合并后重启服务端即可；不改 schema、不动数据。线上插件不需要改：`baize/client/recall.py` 只对 `CANDIDATE_UNVERIFIED` 按策略名分支，新策略名不受影响，新结果仍带 `state`、`living_states` 等字段。
- **未覆盖**：没有所属卡的孤立方案节点不返回任何内容，直接走兜底链；追溯只看 `decided_as`，不沿其它关系类型扩散。

### 修复：夜间整理调模型挪出全局锁，并加整理专用互斥（PR #88，Closes #87）
- **现象**：`POST /v1/sleep/consolidate` 整段持有服务的全局锁，期间每次调用 System 2（批量判研每批、定案提取每组、candidate 核实每条）都挡住召回和写入；也没有整理专用的互斥，重复触发会重复整理。
- **做法**：整理改成三段。阶段一在锁内取内存快照：待处理的草稿行、现役卡、最近 30 分钟更新的实体，以及每张卡的指纹 `(version, updated_at, current_decision)`。阶段二在锁外只读快照做分析并调模型，不再碰共享的数据库连接。阶段三在锁内开写事务（`BEGIN IMMEDIATE`）后先复查：来源草稿行必须仍在、未过期、`phase_hint` 未变，目标卡的指纹必须与快照一致；不满足就整组跳过、草稿行留到下次（没有选择“重新合并”，因为那要再调一次模型，结果仍基于旧判断，跳过不会覆盖刚被改过的内容，代价是最多晚一次固化）。通过复查的部分才写冷归档、更新卡片、删除草稿行。批量判研同样：每批先锁内读待判行，锁外调模型，锁内只对仍是 `judge_status='pending'`、`phase_hint` 仍为空且未过期的行写回。`/v1/sleep/consolidate` 端点不再 `with lock`，改为把全局锁传给 `run_consolidation(..., lock=lock)`；命令行与 MCP 使用各自独立的连接，不传锁。
- **整理互斥**：真实整理走进程内的非阻塞互斥（`DreamConsolidator` 实例上的 `threading.Lock`），拿不到就立即返回 HTTP 200 的 JSON：`status: "busy"`、`message`、`archived_logs: 0`、`updated_states: []`、`archive_file: null`、`skipped_explicit: []`、`batch_judge: {"status": "busy"}`、`candidates: {"verified": 0, "rejected": [], "skipped": []}`，不排队、不重复整理。`dry_run` 不占用互斥（只读预览，不能挡住夜间任务）。
- **返回值**：新增的字段只有两个，`skipped_changed`（`list[object]`，仅在非空时出现，真实整理与 `dry_run` 都可能有；每项是 `{"entity_id": string, "reason": "card_changed" 或 "source_rows_changed"}`，表示该实体这一组复查未通过被整组跳过）和 `message`（`string`，仅在 `status` 为 `busy` 时出现）；`status` 多了一个取值 `busy`。原有字段（`status`、`archived_logs`、`updated_states`、`archive_file`、`skipped_explicit`、`batch_judge`、`candidates`，以及 `dry_run` 下的 `dry_run`、`would_archive_logs`、`would_update_states`、`changes`）的名称、类型和含义不变。
- **dry_run**：仍走三段式，阶段三在写事务里执行真实回写后整体回滚，不写库、不写冷归档、不跑批量判研；阶段二仍会调用 System 2 做分析，所以会产生模型调用。
- **开关**：无新开关。
- **测试**：新增 `test_issue87_consolidate_out_of_lock`（7 例，用假的慢 System 2）：整理进行中并发召回很快返回且不为空；并发写入不丢、不被这次整理误归档，迟到的行下次整理处理；整理期间卡片被改时不覆盖新内容、报告含 `skipped_changed`；两次整理同时触发只跑一次、第二次返回 `busy`；`dry_run` 前后全库逐表一致、冷归档目录无新增；模型报错、超时或回写阶段抛未捕获异常时锁与互斥都能释放，之后能再次整理；批量判研的模型调用不占锁，期间被软删除的行不被写回。生产库只读副本上用假的慢 System 2（每次调用 0.6 秒）、同样输入各跑一次真实整理：整理耗时、模型调用次数、归档条数、更新卡片数都一致；整理期间并发召回的最长等待由 4.81 秒降到 0.01 秒，并发写入的 10 条全部在库、没有被误归档，最长等待由 4.81 秒降到 0.01 秒。该实验没有开启批量判研，批量判研路径只靠测试覆盖。
- **部署**：只重启服务端。线上夜间定时任务通过 HTTP 调用 `/v1/sleep/consolidate`（`curl`，`-m 600` 超时，无 `-f`），请求体和成功时的返回字段都不变；新增的返回形态只有 `busy`（HTTP 200，`curl` 不会判失败）和有跳过时多出的 `skipped_changed`。
- **未覆盖**：互斥只在同一个服务进程、同一个 `DreamConsolidator` 实例内有效；MCP 每次调用新建 `DreamConsolidator`、命令行是独立进程，都不共用这把互斥，靠阶段三的复查保证不覆盖对方的改动，最坏情况是重复花一次模型调用。

### 新增：seed 端点默认拒绝，命令行与 MCP 整理入口接入 System 2（PR #85，Closes #83）
- **背景**：`POST /v1/admin/seed` 会把预置示例卡重置为初始内容，已被夜间整理或手动改过的内容会被覆盖（覆盖前内容有变化时会存一份历史快照）；它在未配置凭据时任何本机调用方都能调。另外，命令行整理入口和 MCP 整理工具没有接入 System 2，只做规则折叠，与 HTTP 路径的配置来源不一致。
- **seed 开关**：`POST /v1/admin/seed` 默认返回 `403`。环境变量 `CORTEX_ALLOW_ADMIN_SEED` 为 `1`、`true` 或 `on`（忽略大小写与首尾空白）才放行，每次请求时读取进程环境，环境变量改动需要重启服务进程才会生效。仍先经过 `authenticate()`：已配置 `CORTEX_AGENT_TOKENS` 时无凭据 `401`、错凭据 `403`，凭据正确但开关关闭仍是 `403`。其它写入接口的鉴权这次没动。
- **System 2 共用配置**：新增 `llm_client_from_env()`（`baize/intelligence/phase_detector.py`），复用 `llm_settings_from_env()`：`CORTEX_LLM_URL` 与 `CORTEX_LLM_KEY` 需同时设置，模型取 `CORTEX_REASONING_MODEL`（未设置时用代码内置默认值），任何一项缺失返回 `None`。HTTP 服务启动（lifespan）、命令行 `python -m baize.core.consolidator` 的 `main()`、MCP 的 `serve()` 都调用它；`BaizeTools` 新增可选参数 `llm_client`，不传时为 `None`。未配置 System 2 时三条入口的行为与此前一致：只做规则折叠，不报错；HTTP 路径行为不变。
- **线上调用情况**：只读排查，没有发现任何程序在调用 seed——Hermes 插件目录、crontab、`/etc/cron.*`、Cortex 相关的 systemd 服务与定时器都是 0 处引用；`baize/` 下 `seed_initial_assets` 只出现在定义处和这个 HTTP 端点，命令行与 MCP 没有 seed 入口；服务日志在本次排查时因权限无法读取，后来由有权限的账号查过：自服务部署以来 seed 只被调用过两次（一次在服务刚部署时，一次在其后几天），没有周期性调用。上线后确认该端点返回 `403`，卡片内容重启前后一致。
- **测试**：新增 `test_issue83_seed_guard_and_system2_entries`（10 例）：开关取值的大小写与空白；默认 `403` 且不改库；开关打开后 `200`；配置 token 时 `401`、`403`、`200`；其它管理端点不受影响；三个入口在同样环境变量下拿到相同的地址、密钥、模型；未配置时三个入口都不报错并走规则折叠；MCP 注入的假模型确实被用于整理；`baize/` 下 seed 只出现在定义处与 HTTP 端点。`test_phase_a_auth_coverage` 与 `test_phase4_recall_router` 里调用 seed 的用例显式设置开关。
- **部署**：更新并重启服务端即可；合并后 seed 默认关闭，线上没有调用方，不受影响。需要手动调用时，在服务环境里设置 `CORTEX_ALLOW_ADMIN_SEED=1` 并重启。线上夜间定时任务走 HTTP，不受命令行与 MCP 改动影响。
- **未覆盖**：命令行没有 seed 入口；其它写入接口（`/v1/living-state/decision`、`/v1/graph/*` 等）在未配置 `CORTEX_AGENT_TOKENS` 时仍不校验凭据，服务只应监听回环地址。

### 文档：README 补安装、配置、启动与升级说明（PR #82，Closes #81）
- **背景**：README 只有架构介绍，新环境要靠读代码和白皮书才能把服务跑起来：服务读取哪些环境变量、数据库与冷归档放在哪、定时任务怎么配、插件怎么部署，都没有集中的说明。
- **做法**：README 新增「快速开始」：环境要求（Python 3.12，依赖只有 `requirements.txt` 里的四项）、建 venv 与装依赖；环境变量表（服务进程与插件进程分开列，写明默认值与作用）；启动命令、默认端口、数据库与冷归档位置（建在进程当前工作目录）、`/health` 自检、MCP stdio 入口；systemd 服务与夜间慢波、草稿清理两个定时任务示例（路径与用户都是占位）；Hermes 插件部署与升级顺序；升级步骤（拉代码、在线备份数据库、重启，新表启动时自动创建）；运行测试的命令。路线图补 #78。地址、模型名、密钥一律用占位符。
- **开关**：无。
- **测试**：合并前在全新目录里完全照 README 走了一遍：新 venv 装依赖、临时数据库、非默认端口启动、`/health`、慢波 `dry_run` 与空请求体、`cortex_purge`（默认与 `--execute`）、MCP `initialize`、备份单行命令、`git pull --ff-only`、README 里的测试循环（33 个测试文件全部通过），然后停进程、删目录。
- **部署**：无。
- **未覆盖**：示例里的 systemd 单元和路径是占位，走查时没有在真实的 systemd 下运行；#85、#90 之后 README 的环境变量与整理说明又有更新，以那两条为准。

### 新增：改主意自动留墓碑、卡片历史与回滚、真正的 consolidate dry_run（PR #79，Closes #78）
- **现象**：（1）只说「改为 Y」并打 approval，夜间整理后卡片升到下一版、内容变成 Y，`tombstones` 却是空的，旧方案没有留下任何记录，只有显式说「推翻 / 作废」才会留；（2）卡片就地覆盖，没有历史，夜里整理错了没法撤回；（3）`POST /v1/sleep/consolidate` 的 `dry_run` 直接返回空报告，看不出今晚会改什么。
- **自动墓碑**：`update_decision` 在新定案替换旧定案时给旧定案留一条墓碑，字段 `plan`、`killed_reason`（自动留的固定为「被新定案取代」）、`version`（被替换的版本号）、`superseded_by`（新定案前 200 字）、`at`（UTC，`YYYY-MM-DD HH:MM:SS`）、`source`（改动来源）、`source_log_ids`（仅夜间整理带，折叠的草稿行 id，最多 20 个）。显式给了 `superseded_plan` 时按它留一条（`killed_reason` 缺省为「推翻更新」），只有它没覆盖旧定案时才另留一条；「覆盖」指文本相等，或一方包含另一方且较短的至少 2 个字（`tombstone_covers`）。新建卡、旧定案为空、仅空白差异、定案没变（只改 facts / 别名 / 展示名）都不留。
- **卡片历史**：新表 `living_state_history`（`CREATE TABLE IF NOT EXISTS` 写进 `schema.sql`，启动时自动创建，不改任何已有表）。卡片内容每次变化前先存一份旧快照：`update_decision`（来源 `consolidation` / `api:decision` / `mcp:update_decision`）、`rename_display`（`backfill_display_names`）、`set_visibility`（`admin:visibility`，可见性没变不写）、`seed_initial_assets`（`seed`，仅当展示名 / 定案 / metadata / 墓碑任一项与预置值不同才存）、回滚（`rollback`）。召回命中计数不算内容变化。`source` 记的是取代该版本的那次改动，不是该版本当初的来源。新增只读接口 `GET /v1/living-state/{entity_id}/history`。
- **回滚**：新增 `POST /v1/admin/living-state/rollback`（鉴权，`dry_run` 默认 `true`），入参 `entity_id`，可选 `to_version`（该版本号最新的一条快照）或 `history_id`（精确指定），二者同时给返回 `422`，目标不存在 `404`；缺省目标是最近一条内容与当前不同的快照。恢复展示名、定案、metadata 与墓碑；版本号继续递增、不复用；回滚前的状态先存一份历史，所以能再滚回去；内容与当前相同（响应 `changed: false`）时不落库、不涨版本；同步现役 `decided_as` 边（只对已有该类边的实体）；不碰草稿流和冷归档，不恢复 `status`。
- **回滚保留当前可见性**：`visibility` 是用户手动设定的访问控制，不是定案内容。按历史恢复会让一次回滚定案悄悄把已收紧的私人卡放回群聊，或把刚放开的卡重新关上。实现上比较内容时忽略 `visibility`，写回时用当前值（响应的 `visibility_kept`）。
- **真正的 dry_run**：`run_consolidation(dry_run=True)` 真跑分析与候选核实，在 `BEGIN IMMEDIATE` 事务里执行与真实整理同一段回写代码，收集变更后整体回滚；不写库、不写冷归档、不跑批量判研。返回 `status: "dry_run"`、`dry_run: true`、`would_archive_logs`、`would_update_states`、`changes`（每张卡一项：`entity_id`、`change`、`version_from`、`version_to`、`decision_from`、`decision_to`、`tombstones_added`、`facts_added`、`aliases_added`、`folded_log_ids`）。#88 之后改为三段式，行为不变，见该条。
- **服务启动不跑种子初始化**：`lifespan` 不调用 `seed_initial_assets`，只有手动调用 `POST /v1/admin/seed` 才会覆盖种子卡；该端点现在默认 `403`，见 #85。种子写入时版本号固定为 1，覆盖后历史里可能出现同一版本号的多条快照，按 `to_version` 回滚取的是该版本号最新的一条，需要精确指定时用 `history_id`。
- **开关**：无新开关。
- **测试**：新增 `test_issue78_tombstone_history_rollback`（14 例）：缓存例子（旧方案留墓碑、历史与 `decided_as` 边都对，回滚回旧方案再滚回新方案）；显式推翻不重复留、定案没变与新建卡不留；各写卡路径写历史并标明来源；`rename_display` 与 seed 只在内容真有变化时写；召回命中计数不写历史；回滚保留当前可见性且不改草稿流；回滚的空操作、错误码、鉴权；`dry_run` 前后全库逐表一致、冷归档目录无新增、无待处理行时可重复、分析失败后无遗留事务；旧库自动补建历史表；旧格式墓碑（没有新字段）仍可加载并回滚。`test_phase_a_auth_coverage` 补历史、回滚两个端点。合并时全量 33 个测试文件通过。
- **部署**：更新并重启服务端即可，历史表在启动时自动创建；线上插件不需要改。历史从部署后才开始记，部署前被覆盖的旧版本找不回来。
- **未覆盖**：历史表只增不删，没有保留与清理策略；回滚只同步 `decided_as` 边，不恢复种子写入时被删掉重建的其它图谱边，也不恢复 `status`；草稿流与冷归档不随回滚变化。

### 维护：测试与文档里的真实标识替换为占位数据（PR #80，无对应 issue）
- **背景**：仓库是公开的，测试和文档里留有真实会话编号、服务器公网地址、人名昵称、真实对话语句，以及随手取用的业务示例名。
- **做法**：只改测试与文档（`CLAUDE.md`、`docs/ARCHITECTURE.md`、`docs/CHANGELOG.md` 和 10 个测试文件），不改 `baize/` 业务源码。会话编号换成明显的假值（如 `20000101_000000_0a0b0c0d`）；服务器地址换成文档保留地址（如 `192.0.2.10`）；真实对话语句改写成意思相近的假句子；人名昵称在测试里换成假名、在文档里换成角色说法（审核方、NAS 侧维护者、项目负责人、架构负责人）；测试里随手取用的业务示例名换成示例名；断言种子数据的用例改为读取种子模块实际写入的值，不再写字面量。改写前用服务器本地的评测标注文件做过整句比对，比对只在本地进行。
- **开关**：无。
- **测试**：改完后全量 32 个测试文件通过；`git grep` 扫描 `baize/` 以外的文件，群名、群 id、真实格式的会话编号、私人卡名、人名昵称均为 0 命中，IP 只剩回环地址。
- **部署**：无需部署。
- **未覆盖**：没有改写 git 历史，旧提交与已合并提交的提交信息里仍保留原值，这是已知的未覆盖项；预置示例数据属于业务代码，保持不变；issue 与 PR 正文的历史编辑记录无法通过接口删除，需要仓库维护者在网页上处理。

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

### 维护：忽略本地评测数据目录（PR #71，Refs #64）
- **背景**：本地评测用的真实对话、人工标注和结果放在 `data/eval/`，此前只靠服务器本机的 `.git/info/exclude` 忽略，换一个 checkout 就可能被误提交。
- **做法**：主干的 `.gitignore` 新增 `data/eval/` 一行，所有 checkout 默认忽略该目录。只改这一行。
- **开关 / 部署**：无。
- **测试**：`git check-ignore -v data/eval/<任一文件>` 命中 `.gitignore`；`git ls-files data/eval` 无输出。
- **未覆盖**：只防止新文件被误提交，已被跟踪的文件不受影响。

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

### 新增：插件白天快速规则产 candidate 并透传 category（PR #61，Issue #56 P2-5，Closes #56）
- **背景**：Issue #56 的最后一项。白天的快速门写死了 5 个关键词，打标规则与夜间批量判研、candidate 候选流不一致，服务端也无法接收 candidate 的类别（事实或定案）。
- **服务端**：`ScratchpadLogIn` 新增可选字段 `category`，`POST /v1/scratchpad/log` 透传给 `append_log(..., category=...)`。校验：只有 `phase_hint=candidate` 且取值为 `fact` / `decision` 时才接收，其它组合返回 `422`；原因是 `scratchpad_log.category` 这一列 `cortex_add` 也在用（`fact` / `preference` / `rule` / `lesson`），不加限制会串味。`ScratchpadManager.append_log` 新增 `category` 参数。不改 schema。
- **客户端**：`CortexClient.sync_turn(user_content, assistant_content, *, session_id, ...)`，签名对齐 Hermes `MemoryProvider` 契约。快速门不再用写死的词，改为引用 `phase_detector` 的 `_APPROVAL_KEYWORDS + _REJECTION_KEYWORDS`（`GATE_KEYWORDS`）。门命中后调用 `detect_phase_rich()`，打标规则：`decision_approved` 且置信度 ≥ 0.9 → `approval`；`decision_approved` 且置信度 < 0.9 → `candidate`（`category=decision`）；`rejection` → `rejection`；其它且 `noul` ≥ 0.8 → `candidate`（`category=fact`）；其它不打标。两个阈值直接引用 `batch_judge.APPROVAL_CONFIDENCE_GATE`（0.9）与 `CANDIDATE_NOUL_GATE`（0.8），与夜间批量判用同一组数。assistant 行永远不打标；判定失败或写入失败只记日志，不阻塞主会话。
- **之后的变化**：#76 起，服务端默认把白天的 `approval` / `rejection` 降为普通行（见 `CORTEX_DAY_JUDGE`），`candidate` 不受影响。
- **开关**：无新开关；`CORTEX_BATCH_JUDGE` 仍保持关闭。
- **测试**：新增 `test_plugin_candidate_category`（5 例）：`append_log` 落库 `category`；端点接收 candidate 的 `category` 并拒收其它组合；`sync_turn` 的完整打标矩阵（含门未命中时不调用模型）；快速门引用标准词表；判定失败时 fail-open。
- **部署**：沿用 #54 的顺序约束：服务端先上，插件后上，反序会让插件写入的 candidate 行静默堆积。仓库里是参考实现，部署副本由部署侧同步。
- **未覆盖**：旧写死词表里的「决定」「确认」「批准」「同意」不在标准词表中，移除后不再触发白天的门；白天的事实类 candidate 只在门命中时产出，不含关键词的句子仍靠夜间批量判。

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
- **红线 1（candidate 候选流）**：高 noul 事实（`noul>=0.8` 且 exploration/chitchat）与「疑似计划」（`decision_approved` 但 `confidence<0.9`——美西反例「下月打算把测试环境迁到新机房」差点变定案）统一进 `candidate`；召回以 `CANDIDATE_UNVERIFIED` 通道降权透出（置信度 0.45，排在 explicit 之后、recent_turns 之前，注入文本带「candidate·未核实」标注）；慢波由 System 2 二次核实——通过并入 `metadata.facts`（携带 noul 溯源），否决归档清退计入报告；**严禁参与定案改写**：不写 `decided_as` 边、不进墓碑、不改写 `current_decision`。
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
- **测试**：`test_recall_single_gram_gate` 重写为红线锁定（白泽记忆反例 `NO_MATCH`、正文单 gram 仍拦、双 gram 不变、具体别名「示例仓库」整词命中 #44 复现句走直达层、全名命中回归保护）；`test_phase6` 补黑名单用例。全量 25 套件通过。
- **验收状态（美西实弹复核后修正，2026-10-24，PR #53）**：**未验收通过——代码层面收益归零**。回撤后门槛与 `302d7cc` 基线一致，「全词命中走直达层」为既有能力而非新增；#44 复现句的通过依赖别名「示例仓库」，而生产库该卡实测 aliases 为 `['流式卡片', '飞书流式推送']`，**不含该别名**，复现句仍落 `RECENT_TURNS_CONTEXT`。本 PR 的实际交付是：撤销噪声放大器 + 别名黑名单防污染。复现句的修复待**别名数据回填**（运维动作：为存量卡补充具体别名，如给「流式卡片」补「示例仓库」）。

### 修复：召回单 2-gram 命中卡名/别名即放行（PR #45，Issue #44 C 项）
- **现象**：卡片就在库里，长句查不到——`recall('当前使用的卡片是示例仓库的最新版吗')` 落到 `RECENT_TURNS_CONTEXT`，权威记忆被末级兜底抢占。根因：无标点中文整句只切出一个原词 token，句中唯一的卡名 2-gram（「卡片」→「流式卡片」）满足不了「≥2 个 2-gram」门槛，整卡被 `DECISION_TEXT_MATCH` 刷掉。
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

### 修复：RECENT_TURNS_CONTEXT 三层收口（PR #40，Issue #39）
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
- NAS 侧维护者已在生产环境实测并完成部署。

### 新增：普通对话轮次末级召回层 RECENT_TURNS_CONTEXT（PR #38，Issue #37）
- **现象**：美西生产实测，老板 `/new` 开新会话后要求验收上一轮改动，分身手里没有任何上一轮上下文，只能回翻会话库。`scratchpad_log` 里当轮 user/assistant 行（id=197/198）都在，但 `/v1/recall` 对「新会话已开启，请继续测试」返回 `NO_MATCH`——**写入正常，召回结构性不通**。
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

### 修复：prefetch 字段契约失配与召回宽匹配抢占兜底（PR #28，Issue #27）
- **现象**：（1）插件每轮自动注入记忆靠读取 `/v1/recall` 响应里的 `living_states`，而服务端当时返回的字典里没有这个字段，解析结果恒为 0 条，自动注入恒为空；（2）`DECISION_TEXT_MATCH` 命中后直接返回，整句自然提问下，未固化的显式记录被掩盖。
- **做法**：`/v1/recall` 所有策略的响应都带 `living_states` 列表（`LIVING_STATE_DIRECT`、`DECISION_TEXT_MATCH`、`PPR_SPREADING`、`SCRATCHPAD_EXPLICIT_FALLBACK`、`NO_MATCH`），由 `state` / `resolved_state` 与未固化记录派生；固化条目 `provisional: false, authoritative: true`，未固化条目 `provisional: true, authoritative: false`。`DECISION_TEXT_MATCH` 命中后不再直接返回，同时探测未固化的显式记录，有则附加 `provisional_facts` 与 `source_log_ids`，策略名与 `state` 不变；没有未固化记录时响应不含这两个字段。选择了「合并返回」，没有走「提高命中阈值」。新增 `baize/client/recall.py` 的 `extract_memories(response)`，解析 `state`、`resolved_state`、`state.metadata.facts`、`living_states`、`provisional_facts`、`facts`，按 id 去重，对 `None`、列表、错误类型等畸形输入一律返回空列表；`CortexClient.prefetch(query, top_k)` 在任何异常或非 2xx 时返回 `[]`（fail-open）。该方法后来在 #73 中改为按会话范围召回。
- **开关**：无。
- **测试**：新增 `test_prefetch_contract_and_fusion`：每种策略都带 `living_states`、宽匹配命中后与显式记录融合、固化后不再作为未固化条目返回、插件侧解析新旧两种响应形态、畸形输入返回空、读取路径只读（断言事务计数不变、无遗留事务）。
- **部署**：改动在仓库的参考实现 `baize/client/` 与服务端 `recall_router.py`；部署副本的插件不在本仓库，服务端补上 `living_states` 后，现有插件读取该字段即可拿到数据，不改插件也行。构建与部署由 NAS 侧维护者负责。
- **未覆盖**：没有在生产副本上复现现场，用同形态的数据在本地复现。

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

### 文档：白皮书升级为 v2.1，建立变更日志与项目约定（PR #24）
- **背景**：Phase 5 至 Phase A 陆续合并后，白皮书、README 和项目约定落后于代码。
- **做法**：`docs/ARCHITECTURE.md` 升级为 v2.1，补 Phase 6 / 7 / 8 / A 的架构说明、Phase A 增量列、时间格式约定、召回路由顺序、环境变量总表、REST 与 MCP 接口总览，并标注 `CORTEX_DECISION_MODEL` 自 Phase 7 起不再使用；新建 `docs/CHANGELOG.md`，按 PR 倒序记录 #2 至 #23；README 路线图补录 Phase 6 / 7 / 8 / A，原路线图里「Phase 6：全量生产平滑割接」的编号已被别名提纯占用，改为不编号的「后续」项；`CLAUDE.md` 补全测试清单与目录约定，写入「每个功能 PR 合并后同步文档并走 PR 合并」和时间戳格式约定（`YYYY-MM-DD HH:MM:SS`，UTC，不用带 `T` 的 ISO 格式）。
- **核对**：环境变量、REST 路径、MCP 工具名、召回路由顺序都对照代码核实。核实时发现服务端没有 429 或 `Retry-After` 逻辑，文档改为「服务端当前不返回 429，仅客户端 Outbox 实现了遵守逻辑」。
- **开关 / 测试 / 部署**：无；纯文档改动，没有重跑测试。

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
