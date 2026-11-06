import json
from datetime import UTC, datetime, timedelta

import pytest
from cmp_worker.sandbox_ops import PREWARM_IMAGES, cleanup_sandboxes, format_status, prewarm_images

from .test_deploy import FakeRunner, make_service

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def ns(name, offset_hours, run="r1"):
    labels = {"cmp.sandbox/run-id": run}
    if offset_hours is not None:
        labels["cmp.sandbox/expires-at"] = str(
            int((NOW + timedelta(hours=offset_hours)).timestamp())
        )
    return {"metadata": {"name": name, "labels": labels}}


def runner_with(*items):
    return FakeRunner(outputs={"get namespaces": json.dumps({"items": list(items)}).encode()})


def test_status_lists_each_sandbox_with_its_run_and_whether_it_has_expired():
    service = make_service(runner_with(ns("cmp-sbx-old", -1, "ra"), ns("cmp-sbx-new", 1, "rb")))
    rows = service.list_sandboxes(now=NOW)
    assert [(r["namespace"], r["run_id"], r["expired"]) for r in rows] == [
        ("cmp-sbx-old", "ra", True),
        ("cmp-sbx-new", "rb", False),
    ]
    text = format_status(rows)
    assert "cmp-sbx-old" in text and "만료" in text and "cmp-sbx-new" in text


def test_a_namespace_that_is_not_a_sandbox_or_has_no_expiry_is_listed_but_never_expired():
    service = make_service(runner_with(ns("kube-system", -5), ns("cmp-sbx-nolabel", None)))
    rows = service.list_sandboxes(now=NOW)
    assert [r["namespace"] for r in rows] == ["cmp-sbx-nolabel"]
    assert rows[0]["expired"] is False


def test_cleanup_deletes_only_expired_sandboxes_and_names_them():
    runner = runner_with(ns("cmp-sbx-old", -1), ns("cmp-sbx-new", 1))
    result = cleanup_sandboxes(make_service(runner), now=NOW)
    assert result == ["cmp-sbx-old"]
    assert [c[c.index("delete") + 2] for c in runner.commands("delete")] == ["cmp-sbx-old"]


def test_cleanup_all_needs_the_exact_confirmation_word():
    runner = runner_with(ns("cmp-sbx-old", -1), ns("cmp-sbx-new", 1))
    with pytest.raises(ValueError):
        cleanup_sandboxes(make_service(runner), now=NOW, everything=True, confirm="yes")
    assert runner.commands("delete") == []
    result = cleanup_sandboxes(make_service(runner), now=NOW, everything=True, confirm="DELETE-ALL")
    assert sorted(result) == ["cmp-sbx-new", "cmp-sbx-old"]


def test_running_cleanup_twice_is_safe():
    runner = runner_with()
    service = make_service(runner)
    assert cleanup_sandboxes(service, now=NOW) == []
    assert cleanup_sandboxes(service, now=NOW) == []


def test_the_command_line_wraps_status_and_cleanup_with_exit_codes():
    from cmp_worker.sandbox_ops import run_command

    runner = runner_with(ns("cmp-sbx-old", -1), ns("cmp-sbx-new", 1))
    service = make_service(runner)
    code, text = run_command(["status"], service, now=NOW)
    assert code == 0 and "cmp-sbx-old" in text
    assert run_command(["cleanup", "--all"], service, now=NOW)[0] == 2
    assert run_command(["nonsense"], service, now=NOW)[0] == 2
    assert runner.commands("delete") == []
    code, text = run_command(["cleanup"], service, now=NOW)
    assert code == 0 and "cmp-sbx-old" in text and "cmp-sbx-new" not in text


def test_prewarm_pulls_each_shared_image_into_the_node_once():
    calls = []

    class Runner:
        def run(self, args, **kwargs):
            calls.append(args)
            return b""

    assert prewarm_images(Runner(), node="n0") == list(PREWARM_IMAGES)
    assert calls == [["docker", "exec", "n0", "crictl", "pull", image] for image in PREWARM_IMAGES]
    assert all("@sha256:" in image for image in PREWARM_IMAGES)
