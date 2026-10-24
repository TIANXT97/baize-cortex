"""Phase 7 专项测试:SystemOne 双门神接力、Fail-open 降级、WAF 防拦截请求头"""
import json
import urllib.request
from unittest import mock

from baize.intelligence.phase_detector import PhaseDetector


class _Resp:
    def __init__(self, payload):
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _answers(phase, noul):
    return {"answers": {"category": {"choice": phase}, "should_memorize": {"noul": noul}}}


def _fake_opener(routes, seen):
    class _Opener:
        def open(self, req, timeout=None):
            seen.append(req)
            outcome = routes[req.full_url]
            if isinstance(outcome, Exception):
                raise outcome
            return _Resp(outcome)

    return _Opener()


DREX = "https://drex.test/v1/systemone"
OPENJEV = "https://openjev.test/v1/systemone"


def _detector(**kwargs):
    return PhaseDetector(
        drex_url=DREX, drex_key="dk", openjev_url=OPENJEV, openjev_key="ok", timeout=1.0, **kwargs
    )


def test_drex_success_returns_high_confidence_approval():
    seen = []
    routes = {DREX: _answers("decision_approved", 0.95)}
    with mock.patch("urllib.request.build_opener", return_value=_fake_opener(routes, seen)):
        phase, conf, category = _detector().detect_phase("就这么定,明天开搞")
    assert (phase, category) == ("decision_approved", "approval")
    assert conf == 0.95
    assert [r.full_url for r in seen] == [DREX]


def test_drex_failure_relays_to_openjev():
    seen = []
    routes = {
        DREX: urllib.error.HTTPError(DREX, 500, "boom", {}, None),
        OPENJEV: _answers("rejection", 0.9),
    }
    with mock.patch("urllib.request.build_opener", return_value=_fake_opener(routes, seen)):
        phase, conf, category = _detector().detect_phase("不对,我记错了")
    assert (phase, category) == ("rejection", "rejection")
    assert [r.full_url for r in seen] == [DREX, OPENJEV]


def test_ambiguous_drex_result_relays_to_openjev():
    seen = []
    routes = {
        DREX: _answers("exploration", 0.5),
        OPENJEV: _answers("decision_approved", 0.9),
    }
    with mock.patch("urllib.request.build_opener", return_value=_fake_opener(routes, seen)):
        phase, _, _ = _detector().detect_phase("这个能不能用?")
    assert phase == "decision_approved"
    assert [r.full_url for r in seen] == [DREX, OPENJEV]


def test_both_gates_down_fail_open_to_local_rules():
    seen = []
    routes = {
        DREX: TimeoutError("drex down"),
        OPENJEV: TimeoutError("openjev down"),
    }
    with mock.patch("urllib.request.build_opener", return_value=_fake_opener(routes, seen)):
        phase, conf, category = _detector().detect_phase("就按这个做")
    assert (phase, category) == ("decision_approved", "approval")
    assert conf == 0.9


def test_requests_carry_waf_safe_headers():
    seen = []
    routes = {DREX: _answers("chitchat", 0.1)}
    with mock.patch("urllib.request.build_opener", return_value=_fake_opener(routes, seen)):
        _detector().detect_phase("哈哈")
    req = seen[0]
    assert req.get_header("User-agent") == "Baize/Gate-v2.0 (TypeSafe-Client)"
    assert req.get_header("Authorization") == "Bearer dk"
    assert req.get_header("Content-type") == "application/json"
    body = json.loads(req.data.decode("utf-8"))
    assert body["questions"]["category"]["type"] == "choice"
    assert body["questions"]["should_memorize"]["type"] == "noul"


def test_local_rules_detect_fullwidth_question_mark():
    phase, conf, category = PhaseDetector().detect_phase("这样行吗？")
    assert (phase, category) == ("exploration", "exploration")
    assert conf == 0.6


if __name__ == "__main__":
    test_drex_success_returns_high_confidence_approval()
    test_drex_failure_relays_to_openjev()
    test_ambiguous_drex_result_relays_to_openjev()
    test_both_gates_down_fail_open_to_local_rules()
    test_local_rules_detect_fullwidth_question_mark()
    test_requests_carry_waf_safe_headers()
    print("✅ Phase 7 SystemOne 双门神专项测试全部通过")
