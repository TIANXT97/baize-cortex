"""单元测试：双时态有效子图切片与 PPR 吸引子滑落验证

实弹验证：
1. 建立 '模具知识库' 实体与两个方案（旧案：人肉拆图；终案：飞桨切片）；
2. 旧案被标记 invalid_at；
3. 即使旧案连接数更多，PPR 在有效子图切片下依然 100% 自动滑落至现役终案。
"""
import sqlite3
from baize.core.temporal_graph import BiTemporalGraphManager
from baize.intelligence.ppr_engine import PPREngine


def test_bitemporal_ppr_convergence():
    conn = sqlite3.connect(":memory:")
    # 加载 schema
    with open("baize/storage/schema.sql", encoding="utf-8") as f:
        conn.executescript(f.read())

    mgr = BiTemporalGraphManager(conn)

    # 1. 创建节点
    mgr.upsert_node("topic:aliyun", "阿里云跳板", "concept")
    mgr.upsert_node("proj:glass_mold", "模具知识库", "project")
    mgr.upsert_node("plan:manual_dict", "人肉拆图方案(已推翻)", "rule")
    mgr.upsert_node("plan:paddle_slice", "飞桨切片终案(现役)", "rule")

    # 2. 建立旧关联（10-18 凌晨），旧方案曾经有很多讨论（高连接度）
    mgr.add_edge("topic:aliyun", "proj:glass_mold", "mentions", "2026-10-18 00:00:00", weight=1.0)
    mgr.add_edge("proj:glass_mold", "plan:manual_dict", "decided_as", "2026-10-18 01:00:00", weight=2.0)
    mgr.add_edge("plan:manual_dict", "proj:glass_mold", "references", "2026-10-18 01:00:00", weight=2.0)

    # 3. 10-18 深夜推翻旧案，敲定飞桨终案
    mgr.invalidate_edge("proj:glass_mold", "plan:manual_dict", "decided_as", "2026-10-18 23:41:00")
    mgr.invalidate_edge("plan:manual_dict", "proj:glass_mold", "references", "2026-10-18 23:41:00")
    mgr.add_edge("proj:glass_mold", "plan:paddle_slice", "decided_as", "2026-10-18 23:41:00", weight=1.0)

    # 4. 导出有效子图
    active_edges = mgr.get_active_subgraph()

    # 5. 用户随口提“阿里云”，注入种子能量
    ppr = PPREngine(damping=0.85)
    energy = ppr.run_ppr(active_edges, seed_nodes={"topic:aliyun": 1.0})

    # 验证断言：
    # a. 飞桨终案获得了能量
    assert "plan:paddle_slice" in energy
    # b. 已作废的人肉拆图方案能量严格为 0（彻底隔离不可达）
    assert energy.get("plan:manual_dict", 0.0) == 0.0
    # c. 能量成功自发滑落至现役终案
    assert energy["plan:paddle_slice"] > 0.0
    print("\n✅ 双时态有效子图隔离与 PPR 吸引子滑落验证通过！")
    print("能量分布:", energy)


if __name__ == "__main__":
    test_bitemporal_ppr_convergence()
