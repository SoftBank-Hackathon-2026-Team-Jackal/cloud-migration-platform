"""Catalog standard tests: health, DB write -> read, data kept across a database restart."""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict

from cmp_testsuite.model import ActionStep, Expect, HttpStep, Suite, SuiteCase

# How long the first request after a database restart may keep being retried while the app recovers.
RESTART_RECOVERY_SECONDS = 60


class AppApi(BaseModel):
    """How the standard tests reach this app; the API list comes from analysis, not guesses."""

    model_config = ConfigDict(extra="forbid")

    health_path: str
    write_path: str | None = None  # POST {id, value}
    read_path: str | None = None  # GET, "{{id}}" is replaced by the written id
    # The pinned suite stays the same while the health path is part of what a fix may correct:
    # the runner then supplies the target's current path as `{{health_path}}`.
    health_from_target: bool = False
    # How the app takes a write and gives it back (ticket 27). Left out, the original shape is used.
    # `write_body` is the POST body ("{{run_id}}" is filled in per run); `write_status` the status a
    # new write answers with; `id_from_response` where the server puts the ID it made. `read_back`
    # GET reads `read_path`; REPEAT_WRITE sends the same write again and expects the stored row
    # (`repeat_status`, the same ID, the same value), which is how an app with an idempotent write
    # shows what it kept. `value_field` is the field holding the written value.
    write_body: dict | None = None
    write_status: int | None = None
    id_from_response: str | None = None
    value_field: str = "value"
    read_back: Literal["GET", "REPEAT_WRITE"] = "GET"
    repeat_status: int = 200


def standard_cases(api: AppApi) -> list[SuiteCase]:
    cases = [
        SuiteCase(
            id="std-health",
            description="The health endpoint answers 200.",
            source="STANDARD",
            effect="READ_ONLY",
            covers=["environment-config"],  # the app starts and answers with its bindings in place
            steps=[
                HttpStep(
                    path="{{health_path}}" if api.health_from_target else api.health_path,
                    expect=Expect(status=200),
                )
            ],
        )
    ]
    repeat = api.read_back == "REPEAT_WRITE"
    if api.write_path and (api.read_path or repeat):
        custom = api.write_body is not None

        def steps(tag: str) -> tuple[HttpStep, HttpStep]:
            # One `run_suite` gives every case the same run ID, and an idempotent write answers 200
            # for a key it already has. So a configured shape gives each case its own key; the
            # original shape stays byte for byte as it was.
            fill = (lambda text: text.replace("{{run_id}}", "{{run_id}}-" + tag)) if custom else str
            body = (
                json.loads(fill(json.dumps(api.write_body)))
                if custom
                else {"id": "std-{{run_id}}", "value": "value-{{run_id}}"}
            )
            value = fill("value-{{run_id}}")
            server_id = api.id_from_response is not None
            write = HttpStep(
                method="POST",
                path=api.write_path,
                json_body=body,
                save={"item_id": api.id_from_response} if server_id else {},
                expect=Expect(status=api.write_status),
            )
            if repeat:
                read = HttpStep(
                    method="POST",
                    path=api.write_path,
                    json_body=body,
                    expect=Expect(
                        status=api.repeat_status,
                        json_equals={api.value_field: value},
                        json_equals_var={api.id_from_response: "item_id"} if server_id else {},
                    ),
                )
            else:
                item = "{{item_id}}" if server_id else fill("std-{{run_id}}")
                read = HttpStep(
                    path=api.read_path.replace("{{id}}", item),
                    expect=Expect(status=200, json_equals={api.value_field: value}),
                )
            return write, read

        write, read = steps("rt")
        restart_write, restart_read = steps("rs")
        cases.append(
            SuiteCase(
                id="std-db-roundtrip",
                description="Data written through the app API is read back unchanged.",
                source="STANDARD",
                effect="WRITES_DATA",
                covers=["postgres"],
                steps=[write, read],
            )
        )
        cases.append(
            SuiteCase(
                id="std-survives-db-restart",
                description="Written data is still there after the database restarts.",
                source="STANDARD",
                effect="WRITES_DATA",
                covers=["postgres"],
                steps=[
                    restart_write,
                    ActionStep(action="RESTART_DATABASE", recover_seconds=RESTART_RECOVERY_SECONDS),
                    restart_read,
                ],
            )
        )
    return cases


def standard_suite(name: str, api: AppApi) -> Suite:
    return Suite(name=name, cases=standard_cases(api))
