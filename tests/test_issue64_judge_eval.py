"""Issue #64:只读抽样、完整分母与启用判据。"""
import json
import os
import tempfile

from baize.core.scratchpad import ScratchpadManager
from baize.intelligence.judge_eval import evaluate, export_dataset, rescore, write_private_json
from baize.storage.db import init_db


def _dataset():
    kinds = ["decision", "fact", "plan", "chitchat", "config"]
    phases = ["decision_approved", "exploration", "exploration", "chitchat", "chitchat"]
    return {
        "annotation": {"status": "reviewed", "reviewer": "test-fixture"},
        "samples": [
            {"id": i + 1, "session_id": f"s{i // 20}", "text": f"样本{i}",
             "kind": kinds[i % 5], "expected_phase": phases[i % 5]}
            for i in range(50)
        ],
    }


class _Echo:
    model = "test-model"

    def __init__(self, dataset, bad_plan=False, broken=False):
        self.rows = {row["id"]: row for row in dataset["samples"]}
        self.bad_plan = bad_plan
        self.broken = broken

    def chat(self, system, user):
        if self.broken:
            return "not JSON"
        values = []
        for row in json.loads(user):
            expected = self.rows[row["id"]]
            phase = "decision_approved" if self.bad_plan and expected["kind"] == "plan" else expected["expected_phase"]
            values.append({"id": row["id"], "phase": phase, "noul": 0.5, "confidence": 0.99})
        return json.dumps(values)


def test_export_is_read_only_reproducible_and_requires_annotation():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "source.db")
        conn = init_db(path)
        try:
            sp = ScratchpadManager(conn)
            for i in range(110):
                sp.append_log(f"s{i // 20}", "user", f"样本{i}")
            conn.execute("UPDATE scratchpad_log SET created_at = '2026-10-25 00:00:00'")
            conn.commit()
            before = conn.execute("SELECT * FROM scratchpad_log ORDER BY id").fetchall()
            one = export_dataset(path, "2026-10-20 00:00:00", "2026-10-27 00:00:00")
            two = export_dataset(path, "2026-10-20 00:00:00", "2026-10-27 00:00:00")
            assert one["samples"] == two["samples"]
            assert len(one["samples"]) == 100 and one["sampling"]["population"] == 110
            assert one["annotation"]["status"] == "unreviewed"
            assert all(row["expected_phase"] is None for row in one["samples"])
            assert conn.execute("SELECT * FROM scratchpad_log ORDER BY id").fetchall() == before
            assert not [name for name in os.listdir(directory) if ".bak-" in name]
        finally:
            conn.close()


def test_activation_requires_review_and_all_trials_pass():
    data = _dataset()
    report = evaluate(data, _Echo(data))
    assert report["judge_gate_passed"] is True and report["blockers"] == []
    assert all(trial["phase_accuracy"] == 1 for trial in report["trials"])
    data["annotation"]["status"] = "unreviewed"
    unreviewed = evaluate(data, _Echo(data))
    assert unreviewed["judge_gate_passed"] is False
    assert unreviewed["dataset_sha256"] == report["dataset_sha256"]
    assert unreviewed["annotation_sha256"] != report["annotation_sha256"]
    data["annotation"]["status"] = "reviewed"
    contaminated = evaluate(data, _Echo(data, bad_plan=True))
    assert contaminated["judge_gate_passed"] is False
    assert all(trial["authority"]["fp"] == 10 for trial in contaminated["trials"])


def test_failed_batches_stay_in_denominator_and_do_not_claim_perfect_precision():
    data = _dataset()
    report = evaluate(data, _Echo(data, broken=True))
    assert report["judge_gate_passed"] is False
    assert all(trial["coverage"] == 0 and trial["phase_accuracy"] == 0 for trial in report["trials"])
    assert all(trial["authority"]["precision"] is None for trial in report["trials"])


def test_opposite_authority_direction_blocks_even_with_98_percent_accuracy():
    data = _dataset()

    class Opposite(_Echo):
        def chat(self, system, user):
            values = json.loads(super().chat(system, user))
            for row in values:
                if row["id"] == 1:
                    row["phase"] = "rejection"
            return json.dumps(values)

    report = evaluate(data, Opposite(data))
    assert report["judge_gate_passed"] is False
    assert all(trial["phase_accuracy"] == 0.98 for trial in report["trials"])
    assert all(trial["authority"]["wrong_phase"] == 1 for trial in report["trials"])


def test_empty_baseline_does_not_claim_agreement_with_later_success():
    data = _dataset()

    class FirstTrialFails(_Echo):
        calls = 0

        def chat(self, system, user):
            self.calls += 1
            return "invalid" if self.calls <= 5 else super().chat(system, user)

    report = evaluate(data, FirstTrialFails(data))
    assert report["trials"][0]["coverage"] == 0
    assert report["trials"][1]["coverage"] == 1
    assert report["trials"][1]["phase_agreement_with_base"] is None
    assert report["trials"][1]["agreement_compared_count"] == 0
    assert report["judge_gate_passed"] is False


def test_rescore_reuses_predictions_but_rejects_changed_samples_or_protocol():
    data = _dataset()
    data["annotation"]["status"] = "unreviewed"
    captured = evaluate(data, _Echo(data))
    data["annotation"]["status"] = "reviewed"
    replay = rescore(data, captured)
    assert replay["judge_gate_passed"] is True
    assert replay["prediction_source"] == "captured; no model calls"
    for trial, original in zip(replay["trials"], captured["trials"]):
        assert trial["predictions"] == original["predictions"]
    data["samples"][0]["text"] += "changed"
    try:
        rescore(data, captured)
    except ValueError:
        pass
    else:
        raise AssertionError("改过的样本仍复用了旧预测")
    data["samples"][0]["text"] = "样本0"
    captured["protocol"]["max_text_chars"] = 100
    try:
        rescore(data, captured)
    except ValueError:
        pass
    else:
        raise AssertionError("改过的协议仍复用了旧预测")


def test_private_outputs_are_owner_only_and_never_overwrite_labels():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "labels.json")
        write_private_json(path, {"reviewer": "original"})
        if os.name != "nt":
            assert os.stat(path).st_mode & 0o777 == 0o600
        try:
            write_private_json(path, {"reviewer": "replacement"})
        except FileExistsError:
            pass
        else:
            raise AssertionError("已复核的标注文件被覆盖")
        with open(path, encoding="utf-8") as stream:
            assert json.load(stream)["reviewer"] == "original"


if __name__ == "__main__":
    test_export_is_read_only_reproducible_and_requires_annotation()
    test_activation_requires_review_and_all_trials_pass()
    test_failed_batches_stay_in_denominator_and_do_not_claim_perfect_precision()
    test_opposite_authority_direction_blocks_even_with_98_percent_accuracy()
    test_empty_baseline_does_not_claim_agreement_with_later_success()
    test_rescore_reuses_predictions_but_rejects_changed_samples_or_protocol()
    test_private_outputs_are_owner_only_and_never_overwrite_labels()
    print("Issue #64 只读评测与启用判据测试通过")
