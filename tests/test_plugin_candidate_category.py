"""Issue #56 P2-5:插件白天快速规则产 candidate 并透传 category,服务端 /v1/scratchpad/log 落库"""
import os
import tempfile

from fastapi.testclient import TestClient

from baize.client.outbox import Outbox
from baize.client.plugin import CortexClient
from baize.core.scratchpad import ScratchpadManager
from baize.server.app import create_app
from baize.storage.db import init_db


class _FakeDetector:
    def __init__(self, verdicts):
        self.verdicts = verdicts
        self.calls = []

    def detect_phase_rich(self, text):
        self.calls.append(text)
        verdict = self.verdicts[text]
        if isinstance(verdict, Exception):
            raise verdict
        phase, noul, confidence = verdict
        return phase, noul, "", confidence


def _transport(tc):
    def transport(url, body, headers, timeout):
        resp = tc.post(url.split("testserver", 1)[1], json=body, headers=headers)
        return resp.status_code, resp.json(), dict(resp.headers)

    return transport


def _client(d, transport, detector):
    ob = Outbox(os.path.join(d, "outbox.jsonl"), clock=lambda: 1000.0, rand=lambda: 1.0)
    return CortexClient("http://testserver", None, "sess", ob, transport=transport, phase_detector=detector)


def _rows(conn):
    return conn.execute(
        "SELECT role, raw_text, phase_hint, category FROM scratchpad_log ORDER BY id"
    ).fetchall()


def test_append_log_persists_category():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(os.path.join(d, "s.db"))
        try:
            row_id = ScratchpadManager(conn).append_log(
                "s", "user", "好,那就这样做", phase_hint="candidate", noul_score=0.2, category="decision"
            )
            row = conn.execute(
                "SELECT phase_hint, category, judge_status FROM scratchpad_log WHERE id = ?", (row_id,)
            ).fetchone()
            assert row == ("candidate", "decision", "done")
            plain = ScratchpadManager(conn).append_log("s", "user", "随便聊聊")
            assert conn.execute(
                "SELECT category FROM scratchpad_log WHERE id = ?", (plain,)
            ).fetchone()[0] is None
        finally:
            conn.close()


def test_scratchpad_log_endpoint_accepts_candidate_category():
    with tempfile.TemporaryDirectory() as d:
        app = create_app(os.path.join(d, "c.db"), os.path.join(d, "cold"))
        with TestClient(app) as tc:
            ok = tc.post("/v1/scratchpad/log", json={
                "session_id": "s", "role": "user", "raw_text": "好,那就这样做",
                "phase_hint": "candidate", "noul_score": 0.2, "category": "decision",
            })
            assert ok.status_code == 200, ok.text
            assert app.state.conn.execute(
                "SELECT phase_hint, category FROM scratchpad_log WHERE id = ?", (ok.json()["id"],)
            ).fetchone() == ("candidate", "decision")
            # category 只服务 candidate 的来源类型,其余组合拒收,防止与 cortex_add 的分类串味
            for extra in (
                {"phase_hint": "candidate", "category": "rule"},
                {"phase_hint": "approval", "category": "decision"},
                {"category": "fact"},
            ):
                bad = tc.post("/v1/scratchpad/log", json={
                    "session_id": "s", "role": "user", "raw_text": "x", **extra,
                })
                assert bad.status_code == 422, (extra, bad.text)


def test_sync_turn_tags_candidates_with_category():
    verdicts = {
        "就按这个记:服务器IP是101.200.45.220": ("exploration", 0.95, 0.7),
        "好,那就这样做": ("decision_approved", 0.2, 0.85),
        "拍板:用方案A": ("decision_approved", 0.1, 0.97),
        "不对,昨天那个结论作废": ("rejection", 0.9, 0.95),
    }
    with tempfile.TemporaryDirectory() as d:
        app = create_app(os.path.join(d, "c.db"), os.path.join(d, "cold"))
        with TestClient(app) as tc:
            detector = _FakeDetector(verdicts)
            cc = _client(d, _transport(tc), detector)
            cc.sync_turn("就按这个记:服务器IP是101.200.45.220", "好的,已记下", session_id="s1")
            for text in ("好,那就这样做", "拍板:用方案A", "不对,昨天那个结论作废", "今天天气不错"):
                cc.sync_turn(text, "", session_id="s1")

            assert _rows(app.state.conn) == [
                ("user", "就按这个记:服务器IP是101.200.45.220", "candidate", "fact"),
                ("assistant", "好的,已记下", None, None),
                ("user", "好,那就这样做", "candidate", "decision"),
                ("user", "拍板:用方案A", "approval", None),
                ("user", "不对,昨天那个结论作废", "rejection", None),
                ("user", "今天天气不错", None, None),
            ]
            # 门用标准词表:「拍板」「作废」「就这样做」不在旧 5 词内也能开门;闲聊不叫模型
            assert "今天天气不错" not in detector.calls
            assert "拍板:用方案A" in detector.calls and "不对,昨天那个结论作废" in detector.calls


def test_gate_references_standard_keywords():
    from baize.client.plugin import GATE_KEYWORDS
    from baize.intelligence.phase_detector import _APPROVAL_KEYWORDS, _REJECTION_KEYWORDS

    assert set(GATE_KEYWORDS) == set(_APPROVAL_KEYWORDS) | set(_REJECTION_KEYWORDS)


def test_sync_turn_is_fail_open():
    with tempfile.TemporaryDirectory() as d:
        app = create_app(os.path.join(d, "c.db"), os.path.join(d, "cold"))
        with TestClient(app) as tc:
            detector = _FakeDetector({"拍板用方案A": RuntimeError("门神挂了")})
            _client(d, _transport(tc), detector).sync_turn("拍板用方案A", "", session_id="s1")
            assert _rows(app.state.conn) == [("user", "拍板用方案A", None, None)]

        def down(url, body, headers, timeout):
            raise TimeoutError("3s")

        _client(d, down, _FakeDetector({})).sync_turn("今天天气不错", "好的", session_id="s1")


if __name__ == "__main__":
    test_append_log_persists_category()
    test_scratchpad_log_endpoint_accepts_candidate_category()
    test_sync_turn_tags_candidates_with_category()
    test_gate_references_standard_keywords()
    test_sync_turn_is_fail_open()
    print("✅ 插件 candidate 打标与 category 透传测试全部通过")
