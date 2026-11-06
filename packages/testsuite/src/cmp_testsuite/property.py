"""A write shape read from the source becomes property tests (ticket 27-B).

The Tester says how the app takes a write and gives it back, and cites the source lines. Tests are
then built from the same templates as the standard tests, so every expected value is one the test
wrote itself: nothing here is an expected value a model made up.
"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from cmp_testsuite.model import Evidence, SuiteCase, _Strict
from cmp_testsuite.standard import AppApi, standard_cases

VALUE_MARKER = "value-{{run_id}}"


class ShapeProposal(_Strict):
    id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=40)]
    write_path: Annotated[str, Field(pattern=r"^/[^\s{}]*$")]
    # The POST body. The field named by `value_field` must be exactly "value-{{run_id}}": the test
    # writes that value and expects to read the very same one back. Other fields hold what the
    # code requires to accept a write.
    write_body: dict
    write_status: Annotated[int, Field(ge=100, le=599)]
    id_from_response: str | None = None  # where the server puts the ID it made, if it makes one
    value_field: str = "value"
    read_back: Literal["GET", "REPEAT_WRITE"] = "GET"
    read_path: str | None = None  # for GET: "{{id}}" stands for the written ID
    repeat_status: Annotated[int, Field(ge=100, le=599)] = 200
    evidence: list[Evidence] = Field(min_length=1)

    @model_validator(mode="after")
    def _make_a_sound_test(self) -> "ShapeProposal":
        if self.write_body.get(self.value_field) != VALUE_MARKER:
            raise ValueError(f"write_body['{self.value_field}'] must be exactly '{VALUE_MARKER}'")
        if self.read_back == "GET" and not (self.read_path and "{{id}}" in self.read_path):
            raise ValueError("read_back GET needs a read_path that contains '{{id}}'")
        if (
            self.read_back == "GET"
            and self.id_from_response is None
            and self.write_body.get("id") != "std-{{run_id}}"
        ):
            # Without a server-made ID the test must choose it, so it can read the same one back.
            raise ValueError("a client-chosen ID needs write_body['id'] to be 'std-{{run_id}}'")
        if self.read_path is not None and not self.read_path.startswith("/"):
            raise ValueError("read_path must start with '/'")
        return self


def property_cases(shape: ShapeProposal) -> list[SuiteCase]:
    """The round-trip and restart tests for this shape, as AI tests citing the shape's source."""
    api = AppApi(
        health_path="/",  # the health test is dropped below; the Tester does not own it
        write_path=shape.write_path,
        read_path=shape.read_path,
        write_body=shape.write_body,
        write_status=shape.write_status,
        id_from_response=shape.id_from_response,
        value_field=shape.value_field,
        read_back=shape.read_back,
        repeat_status=shape.repeat_status,
    )
    names = {"std-db-roundtrip": "roundtrip", "std-survives-db-restart": "restart"}
    built = []
    for case in standard_cases(api):
        if case.id not in names:
            continue
        built.append(
            case.model_copy(
                update={
                    "id": f"ai-{shape.id}-{names[case.id]}",
                    "source": "AI",
                    "evidence": list(shape.evidence),
                }
            )
        )
    return built
