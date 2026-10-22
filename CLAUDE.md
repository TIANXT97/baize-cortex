# CLAUDE.md — 白泽·Cortex 项目级指令

> 仓库:`TIANXT97/baize-cortex`。本文件对本仓库内所有会话生效,补充工位级 CLAUDE.md 的通用规则。

---

## 项目定位

白泽·Cortex 是单文件嵌入式的 AI Agent 长期记忆中枢。四层结构:Layer 0 草稿流、Layer 1 活体状态卡、Layer 2 双时态图谱与 PPR、Layer 3 慢波合成与冷归档。入口为 FastAPI(`baize/server/app.py`)与 MCP stdio(`baize/server/mcp_server.py`)。

## 架构铁律

- 存储只用 SQLite(WAL 模式,`PRAGMA foreign_keys=ON`,`synchronous=NORMAL`),零外部中间件(不引入 Redis、Kafka、Neo4j 等)。
- 依赖只写在 `requirements.txt`。
- 不物理删除数据:热表清退前必须先无损写入 `cold_archive/*.jsonl.gz`。
- MCP 层为手写 JSON-RPC 2.0(协议 2024-11-05),暂不引入官方 `mcp` 库。
- 双时态边用 `valid_at` / `invalid_at` 表达,PPR 只在 `v_active_graph_edges` 视图上运行。

## 目录约定

- `baize/core/`:Layer 0–3 的管理器与 `seeds.py`
- `baize/intelligence/`:`phase_detector.py`(System 1)、`ppr_engine.py`、`recall_router.py`
- `baize/storage/`:`schema.sql`、`db.py`、`migrations.py`(只做增量加法迁移)
- `baize/client/`:cortex 插件参考实现(Outbox、`cortex_add`、镜像钩子、熔断),只用标准库
- `baize/server/`:REST 与 MCP 入口
- `tests/`:按阶段命名的集成测试,每个文件都支持 `python tests/xxx.py` 直接运行

## 测试与验证

在仓库根目录执行(需设置 `PYTHONPATH=.`):

```bash
python tests/test_bitemporal.py
python tests/test_phase1_mvp.py
python tests/test_phase2_mcp_graph.py
python tests/test_phase3_consolidation.py
python tests/test_phase4_recall_router.py
python tests/test_phase5_hardening.py
python tests/test_phase6_semantic_alias.py
python tests/test_phase7_systemone_gates.py
python tests/test_phase8_consolidation_closure.py
python tests/test_phase_a_migration.py   # 另有 test_phase_a_{outbox,cortex_add,normalization,mirror,circuit_breaker}.py
python -m py_compile <改动的文件>
```

- 解释器统一用 Python 3.12(需已安装 `fastapi`、`httpx`)。工位 `python3` 指向 3.13 且未装依赖,不要用它跑测试。
- Windows 下测试临时目录清理前必须关闭 SQLite 连接(用 `try/finally`)。
- 读写 `schema.sql` 等文件时显式指定 `encoding="utf-8"`,避免 GBK 默认编码导致解码失败。

## 分支与 PR 流程

1. 开工前:`git checkout main && git pull origin main`,再从 `main` 切出 `feature/*` 或 `docs/*` 分支。
2. 自测全绿后推送分支(只走 SSH:`git@github.com:TIANXT97/baize-cortex.git`)并创建 PR。
3. PR 描述必须贴出测试输出,并写 `Closes #<issue>`。
4. 合并由审核方(夭夭)以 squash 方式完成,合并后同步本地 `main` 并删除本地功能分支。
5. 进度与问题在对应 issue 上回复,不在 PR 之外单独提问架构问题。

## 红线

- 不 `--force` 推送,不 `reset --hard` `main`。
- 不把 token 写进 git remote URL。
- 不恢复 ghproxy.net URL 重写。
- 构建、容器重启、数据库迁移、NAS 部署由 NAS 侧(夭夭)负责,本仓库会话不执行。

## 文档约定

- 代码默认不写注释;只在"为什么"不明显时写一行。
- 每个功能 PR 合并后,必须跟一个 `docs/*` 分支同步 `README.md`(路线图、目录结构)、`docs/ARCHITECTURE.md` 与 `docs/CHANGELOG.md`,同样走 PR 并合并,保证项目演进有迹可循。
- 时间戳统一 `YYYY-MM-DD HH:MM:SS`(UTC),不使用带 `T` 的 ISO 格式,否则会破坏 `v_active_graph_edges` 的字符串比较。
- 不新增规划类或分析类文档,除非用户明确要求。
