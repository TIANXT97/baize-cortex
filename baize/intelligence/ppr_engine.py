"""吸引子能量扩散检索引擎 (Personalized PageRank Engine)

基于有效子图进行概率能量游走，实现模糊线索向现役核心定案的自动收敛。
"""
from __future__ import annotations
from typing import Dict, List, Tuple
from collections import defaultdict


class PPREngine:
    def __init__(self, damping: float = 0.85, max_iter: int = 40, tol: float = 1e-5):
        self.damping = damping
        self.max_iter = max_iter
        self.tol = tol

    def run_ppr(
        self,
        active_edges: List[Tuple[str, str, float]],
        seed_nodes: Dict[str, float]
    ) -> Dict[str, float]:
        """在给定的有效子图上运行 PPR 算法。

        :param active_edges: (src, dst, weight) 列表（必须已过滤失效边）
        :param seed_nodes: 初始能量注入节点字典 {node_id: weight}
        :return: 稳态能量分布 {node_id: energy_score}
        """
        if not seed_nodes:
            return {}

        # 1. 归一化初始种子能量分布
        total_seed = sum(seed_nodes.values())
        p = {node: w / total_seed for node, w in seed_nodes.items()}

        # 2. 构建有向图邻接表
        adj = defaultdict(dict)
        out_degree = defaultdict(float)
        all_nodes = set(seed_nodes.keys())

        for src, dst, w in active_edges:
            adj[src][dst] = w
            out_degree[src] += w
            all_nodes.add(src)
            all_nodes.add(dst)

        if not all_nodes:
            return {}

        # 3. 幂迭代求解
        # 初始状态设为与种子能量一致
        r = {node: p.get(node, 0.0) for node in all_nodes}

        for _ in range(self.max_iter):
            next_r = {node: 0.0 for node in all_nodes}
            # 游走传播
            for src in all_nodes:
                if out_degree[src] > 0:
                    src_val = r[src]
                    for dst, w in adj[src].items():
                        next_r[dst] += self.damping * src_val * (w / out_degree[src])
                else:
                    # 悬挂节点能量回灌给种子
                    for s_node, s_prob in p.items():
                        next_r[s_node] += self.damping * r[src] * s_prob

            # 重启注入能量
            for s_node, s_prob in p.items():
                next_r[s_node] += (1.0 - self.damping) * s_prob

            # 收敛性校验
            diff = sum(abs(next_r[n] - r[n]) for n in all_nodes)
            r = next_r
            if diff < self.tol:
                break

        return dict(sorted(r.items(), key=lambda x: x[1], reverse=True))
