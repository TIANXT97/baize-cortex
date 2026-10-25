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
│   │   ├── living_state.py    # Layer 1: 项目活体状态卡管理器
│   │   ├── temporal_graph.py  # Layer 2: 双时态图谱与有效子图切片
│   │   ├── scratchpad.py      # Layer 0: 瞬态草稿流
│   │   ├── consolidator.py    # Layer 3: 夜间慢波睡眠合成引擎
│   │   ├── normalization.py   # 内容规范化与幂等键 (客户端/服务端共用)
│   │   ├── supersession.py    # supersedes_id 校验与旧记录失效
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
│   │   ├── plugin.py          # cortex_add 与 on_memory_write 镜像钩子
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
- [ ] **Issue #56: 批量判研二审排险** —— 已完成 PR 1（切批空批防护、批量判步骤异常隔离不再击杀慢波折叠）；待办 PR 2（candidate 核实按事实类/定案类分流）与 confidence 线大样本定线，完成前 `CORTEX_BATCH_JUDGE` 保持 off
- [ ] **后续: 全量生产平滑割接** —— 现役存量数据提纯与全场景持续演化(原路线图中的 Phase 6 编号已被别名提纯占用,此项改为不编号待排期)

---

## 📜 开源协议

本项目基于 **Apache-2.0** 协议开源。
