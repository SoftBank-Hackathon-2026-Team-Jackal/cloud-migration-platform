"""Catalog standard tests: health, DB write -> read, data kept across a database restart."""

from pydantic import BaseModel, ConfigDict

from cmp_testsuite.model import ActionStep, Expect, HttpStep, Suite, SuiteCase


class AppApi(BaseModel):
    """How the standard tests reach this app; the API list comes from analysis, not guesses."""

    model_config = ConfigDict(extra="forbid")

    health_path: str
    write_path: str | None = None  # POST {id, value}
    read_path: str | None = None  # GET, "{{id}}" is replaced by the written id


def standard_cases(api: AppApi) -> list[SuiteCase]:
    cases = [
        SuiteCase(
            id="std-health",
            description="The health endpoint answers 200.",
            source="STANDARD",
            effect="READ_ONLY",
            steps=[HttpStep(path=api.health_path, expect=Expect(status=200))],
        )
    ]
    if api.write_path and api.read_path:
        write = HttpStep(
            method="POST",
            path=api.write_path,
            json_body={"id": "std-{{run_id}}", "value": "value-{{run_id}}"},
            expect=Expect(status=None),
        )
        read_path = api.read_path.replace("{{id}}", "std-{{run_id}}")
        read = HttpStep(
            path=read_path,
            expect=Expect(status=200, json_equals={"value": "value-{{run_id}}"}),
        )
        cases.append(
            SuiteCase(
                id="std-db-roundtrip",
                description="Data written through the app API is read back unchanged.",
                source="STANDARD",
                effect="WRITES_DATA",
                steps=[write, read],
            )
        )
        cases.append(
            SuiteCase(
                id="std-survives-db-restart",
                description="Written data is still there after the database restarts.",
                source="STANDARD",
                effect="WRITES_DATA",
                steps=[write, ActionStep(action="RESTART_DATABASE"), read],
            )
        )
    return cases


def standard_suite(name: str, api: AppApi) -> Suite:
    return Suite(name=name, cases=standard_cases(api))
