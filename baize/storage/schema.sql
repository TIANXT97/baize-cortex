-- 白泽 2.0 核心四层正交物理数据库 Schema
-- 严格基于 SQLite 3.38+ WAL 模式

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================================
-- Layer 0: 瞬态草稿流 (Episodic Scratchpad)
-- 承载会话期的全部未经清洗的交互与排障碎屑，微秒级无锁追加写
-- ============================================================================
CREATE TABLE IF NOT EXISTS scratchpad_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,                  -- 'user' | 'assistant' | 'system'
    raw_text TEXT NOT NULL,
    phase_hint TEXT,                     -- 由 System 1 (Drex-Max) 打上的相变候选标: 'approval' | 'rejection' | 'exploration'
    noul_score REAL DEFAULT 0.0,         -- 显著性得分
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_scratchpad_session ON scratchpad_log(session_id);
CREATE INDEX IF NOT EXISTS idx_scratchpad_phase ON scratchpad_log(phase_hint) WHERE phase_hint IS NOT NULL;

-- ============================================================================
-- Layer 1: 项目活体状态卡 (Living State Vault)
-- 系统最高优先级现役权威真理，一项目一卡，硬上限 20~30 个实体
-- ============================================================================
CREATE TABLE IF NOT EXISTS living_states (
    entity_id TEXT PRIMARY KEY,          -- 例如 'proj:glass_mold_knowledge', 'proj:10router'
    display_name TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,                -- 'ACTIVE' | 'ARCHIVED' | 'BLOCKED'
    current_decision TEXT NOT NULL,      -- 当前唯一生效的终审定案说明
    metadata_json TEXT DEFAULT '{}',     -- 核心参数、架构依赖等结构化快照
    tombstones_json TEXT DEFAULT '[]',   -- 已被推翻作废的历史方案与否决原因 (Array of objects)
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================================
-- Layer 2: 双时态实体拓扑图 (Bi-temporal Graph)
-- 实体节点与带有现实有效时态、系统认知时态的有向加权关系网
-- ============================================================================
CREATE TABLE IF NOT EXISTS graph_nodes (
    node_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    node_type TEXT NOT NULL,             -- 'project' | 'concept' | 'tech_stack' | 'rule'
    properties_json TEXT DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS graph_edges (
    edge_id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_id TEXT NOT NULL,
    dst_id TEXT NOT NULL,
    rel_type TEXT NOT NULL,              -- 'depends_on' | 'decided_as' | 'superseded_by' | 'belongs_to'
    weight REAL DEFAULT 1.0,
    valid_at TIMESTAMP NOT NULL,         -- 事实在现实中的生效时间
    invalid_at TIMESTAMP,                -- 事实在现实中的失效时间 (NULL 代表当前现役)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expired_at TIMESTAMP,
    FOREIGN KEY(src_id) REFERENCES graph_nodes(node_id) ON DELETE CASCADE,
    FOREIGN KEY(dst_id) REFERENCES graph_nodes(node_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_edges_active ON graph_edges(src_id, dst_id) WHERE invalid_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_edges_temporal ON graph_edges(valid_at, invalid_at);

-- 核心现役有效子图视图 (Active Subgraph View)
-- PPR 能量游走引擎严格只对本视图进行遍历，彻底屏蔽已作废历史边
DROP VIEW IF EXISTS v_active_graph_edges;
CREATE VIEW v_active_graph_edges AS
SELECT edge_id, src_id, dst_id, rel_type, weight, valid_at
FROM graph_edges
WHERE invalid_at IS NULL AND valid_at <= datetime('now');
