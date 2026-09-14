"""Tests for the version-to-version diff and its exit code (no infra required).

회귀 판정은 하네스가 존재하는 이유다. 그래서 DB 없이도 돌아가야 하고, DB 없이
검증돼야 한다 — CI 가 실제로 실행하는 것이 이 경로다.
"""

import json

import pytest

from eval import harness, report as report_lib


def result(qid, score, rank, qtype="single_fact", split="tune"):
    return report_lib.QuestionResult(
        question_id=qid,
        question_type=qtype,
        split=split,
        first_gold_rank=rank,
        metrics={"R@1": 1.0 if rank == 1 else 0.0, "R@10": score, "MRR": 1 / rank if rank else 0.0},
    )


def run(results, *, config="hybrid+rerank", k=10, sha="aaa111", digest="d" * 64):
    return report_lib.RunReport(
        dataset_name="set",
        dataset_sha256=digest,
        config=config,
        k=k,
        git_sha=sha,
        num_questions=len(results),
        num_scored=len(results),
        metrics=report_lib.aggregate(results, ["R@1", "R@10", "MRR"]),
        results=results,
    )


def test_identical_runs_have_no_regression():
    rows = [result("q1", 1.0, 1), result("q2", 1.0, 2)]
    diff = report_lib.diff(run(rows), run(rows))

    assert diff.has_regression is False
    assert diff.regressions == []
    assert all(abs(d.delta) < report_lib.EPS for d in diff.deltas)


def test_flipped_question_is_named_even_when_the_average_holds():
    # 하나 좋아지고 하나 나빠져 평균이 그대로인 경우. 이걸 통과시키면 하네스는
    # 정확히 존재 이유를 놓친다.
    base = run([result("q1", 1.0, 1), result("q2", 0.0, None)])
    head = run([result("q1", 0.0, None), result("q2", 1.0, 1)])

    diff = report_lib.diff(base, head)

    assert [f.question_id for f in diff.regressions] == ["q1"]
    assert [f.question_id for f in diff.improvements] == ["q2"]
    assert diff.has_regression is True
    assert abs(diff.deltas[1].delta) < report_lib.EPS  # R@10 평균은 그대로


def test_aggregate_drop_is_a_regression_even_without_a_flip():
    base = run([result("q1", 1.0, 1)])
    head = run([result("q1", 1.0, 3)])  # R@10 은 같고 MRR 만 떨어진다

    diff = report_lib.diff(base, head)

    assert diff.regressions == []
    assert [d.key for d in diff.metric_regressions] == ["R@1", "MRR"]
    assert diff.has_regression is True


def test_tolerance_absorbs_a_small_aggregate_drop():
    # 정답 순위만 2위에서 3위로 밀린 경우 — MRR 만 0.167 움직인다.
    base = run([result("q1", 1.0, 2)])
    head = run([result("q1", 1.0, 3)])

    assert report_lib.diff(base, head, tolerance=0.2).has_regression is False
    assert report_lib.diff(base, head, tolerance=0.1).has_regression is True


def test_refuses_to_compare_different_measurements():
    rows = [result("q1", 1.0, 1)]
    with pytest.raises(report_lib.IncomparableRuns, match="구성이 다르다"):
        report_lib.diff(run(rows), run(rows, config="dense"))
    with pytest.raises(report_lib.IncomparableRuns, match="cutoff"):
        report_lib.diff(run(rows), run(rows, k=5))
    with pytest.raises(report_lib.IncomparableRuns, match="데이터셋 내용이 바뀌었다"):
        report_lib.diff(run(rows), run(rows, digest="e" * 64))


def test_dataset_change_can_be_forced_through():
    rows = [result("q1", 1.0, 1)]
    diff = report_lib.diff(run(rows), run(rows, digest="e" * 64), strict=False)
    assert diff.has_regression is False


def test_added_and_dropped_questions_are_reported():
    base = run([result("q1", 1.0, 1)])
    head = run([result("q2", 1.0, 1)])

    diff = report_lib.diff(base, head, strict=False)

    assert diff.dropped == ["q1"]
    assert diff.added == ["q2"]


def test_report_round_trips_through_json():
    original = run([result("q1", 1.0, 1)])
    restored = report_lib.RunReport.from_dict(json.loads(json.dumps(original.to_dict())))

    assert restored.metrics == original.metrics
    assert restored.results[0].question_id == "q1"


# --- CLI ---


def _write(tmp_path, name, report):
    path = tmp_path / name
    path.write_text(json.dumps([report.to_dict()], ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_diff_cli_exits_nonzero_on_regression(tmp_path, capsys):
    base = _write(tmp_path, "base.json", run([result("q1", 1.0, 1)]))
    head = _write(tmp_path, "head.json", run([result("q1", 0.0, None)]))

    code = harness.main(["diff", "--base-json", base, "--head-json", head])

    assert code == 1
    out = capsys.readouterr().out
    assert "회귀" in out
    assert "q1" in out


def test_diff_cli_exits_zero_when_nothing_moved(tmp_path, capsys):
    rows = [result("q1", 1.0, 1)]
    base = _write(tmp_path, "base.json", run(rows))
    head = _write(tmp_path, "head.json", run(rows))

    assert harness.main(["diff", "--base-json", base, "--head-json", head]) == 0
    assert "회귀 없음" in capsys.readouterr().out


def test_diff_cli_picks_the_requested_config(tmp_path):
    path = tmp_path / "runs.json"
    path.write_text(
        json.dumps(
            [run([result("q1", 1.0, 1)], config="dense").to_dict(),
             run([result("q1", 1.0, 1)]).to_dict()],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert harness.main(
        ["diff", "--config", "dense", "--base-json", str(path), "--head-json", str(path)]
    ) == 0


def test_bare_dataset_flag_means_run(monkeypatch):
    """``python -m eval.harness --dataset ...`` 는 run 으로 읽힌다."""
    seen = {}

    async def fake_run(args):
        seen["dataset"] = args.dataset
        seen["config"] = args.config
        return 0

    monkeypatch.setattr(harness, "cmd_run", fake_run)
    assert harness.main(["--dataset", "x.jsonl"]) == 0
    assert seen == {"dataset": "x.jsonl", "config": [harness.DEFAULT_CONFIG]}
