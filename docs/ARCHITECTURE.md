# 🏛️ 白泽·皮层 (Bai Ze Cortex) 架构设计白皮书

**工程标识**：`baize-cortex`  
**文档版本**：v2.0-Draft  
**审定人**：田总监 (TIAN)  
**架构执笔**：夭夭 (CTO & Chief Architect)  
**日期**：2026-10-20  

---

## 1. 认知哲学与范式跃迁

白泽 1.x 采用的“基于平铺表（Flat Table）的追加存储 + 向量检索”机制，在面对长周期多轮工程对话时，必然陷入三大致命困境：
1. **记忆污染（Memory Pollution）**：大量调试、报错、瞬态确认被视作等权事实入库，冲垮时效池；
2. **时态塌陷（Temporal Collapse）**：新旧方案在库内平权共存，检索引擎无法物理识别被推翻的陈年方案；
3. **联想僵化（Associative Rigidity）**：单步余弦相似度无法处理带有偏置或错词的跨跳工程检索。

白泽·皮层（baize-cortex）确立了**“仿生认知双轨”**与**“双时态状态机”**的核心哲学：
- **人类记忆本质是模式补全（Pattern Completion）与重构（Reconstruction）**，不是录像带回放；
- **遗忘与睡眠修剪不是缺陷，而是系统维持抗熵增的最高算法**；
- **重大工程方案存在唯一现役真理**，必须与历史演进拓扑物理解耦。

---

## 2. 四层物理正交存储模型 (Storage Topology)

底座坚持**纯嵌入式、单文件 SQLite WAL 模式**，内存预算 500MB~1GB。

### 2.1 Layer 0: 瞬态草稿流 (Episodic Scratchpad)
- **表结构**：`scratchpad_log (id, session_id, role, raw_text, phase_hint, created_at)`
- **特性**：微秒级无锁追加，零向量化，零阻断。
- **生命周期**：仅在当前活跃会话与当日保留，夜间睡眠阶段执行冷归档。

### 2.2 Layer 1: 项目活体状态卡 (Living State Vault)
- **表结构**：`living_states (entity_id PRIMARY KEY, version, current_decision, status, tombstones_json, updated_at)`
- **特性**：一项目一文档，硬上限限制在 20~30 个核心工程实体。
- **查询复杂度**：$O(1)$ 主键直连，直接旁路所有图检索与向量比对。

### 2.3 Layer 2: 双时态实体拓扑图 (Bi-temporal Graph)
- **表结构**：
  - `graph_nodes (node_id PRIMARY KEY, label, properties_json, created_at)`
  - `graph_edges (edge_id PRIMARY KEY, src_id, dst_id, rel_type, weight, valid_at, invalid_at, created_at, expired_at)`
- **时态语义**：
  - `valid_at` / `invalid_at`：事实在现实世界中的有效时间区间；
  - `created_at` / `expired_at`：系统记录与标记该事实的时间区间；
- **切片规则**：检索时必须强制执行有效子图切片（Active Subgraph Slice）：
  $$\mathcal{G}_{active} = \{e \in \mathcal{E} \mid e.invalid\_at \text{ IS NULL}\}$$

### 2.4 Layer 3: 慢波合成睡眠 (Dream Consolidation)
- **机制**：凌晨无监督离线批处理。
- **流程**：
  1. 扫描当日 `scratchpad_log` 中由 System 1 标记的相变锚点；
  2. 结合深度模型执行跨对话折叠，提炼高阶长效共识；
  3. 执行 `UPDATE living_states` 与 `UPDATE graph_edges SET invalid_at = NOW()`；
  4. 将原日志打包压缩归档至 `cold_archive/`，热表清空。

---

## 3. 检索与计算引擎：有效子图上的 PPR 能量扩散

为解决单步向量检索面对模糊词或偏置词抓瞎的问题，白泽 2.0 引入基于 Personalized PageRank 的吸引子滑落机制：

1. **种子能量注入**：将用户输入的实体词或关键词映射为拓扑图中的种子节点集 $S$，分配初始能量向量 $\mathbf{p}$；
2. **带阻尼的概率游走**：
   $$\mathbf{r}^{(k+1)} = (1 - \alpha) \mathbf{P}^T \mathbf{r}^{(k)} + \alpha \mathbf{p}$$
   其中 $\mathbf{P}$ 为有效子图 $\mathcal{G}_{active}$ 的转移概率矩阵，$\alpha$ 为重启阻尼因子（取 0.15）；
3. **物理隔离旧方案**：由于已推翻方案的边在 $\mathcal{G}_{active}$ 中已被切断，能量无法向失效节点扩散，自然避免了“历史热搜偏见”，能量自发汇聚到连接最密集的现役核心定案。

---

## 4. 双系统协同 (Dual-System Governance)

- **System 1 (直觉层 · 毫秒级哨兵)**：
  - 核心引擎：`Drex-Max`（推断耗时 ~24ms，端到端 ~300ms）；
  - 职责：会话入口意图分类、瞬时相变嗅探（捕捉拍板与否定）；
- **System 2 (深思层 · 离线淬火引擎)**：
  - 核心引擎：`DeepSeek / GLM / Kimi / Claude`；
  - 职责：夜间慢波睡眠抽象提炼、复杂矛盾全局仲裁、白皮书生成。
