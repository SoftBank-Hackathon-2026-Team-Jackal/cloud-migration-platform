"""Test suite definition: HTTP scenarios plus the metadata the runner and validator need.

Names avoid a `Test` prefix so pytest never collects them.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Expect(_Strict):
    status: int | None = None
    # dotted path into the JSON body -> expected literal value
    json_equals: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    # dotted path -> saved variable name; the body value must equal what an earlier step saved
    json_equals_var: dict[str, str] = Field(default_factory=dict)
    body_contains: str | None = None

    def has_value_assertion(self) -> bool:
        return bool(self.json_equals or self.json_equals_var or self.body_contains)


class HttpStep(_Strict):
    action: Literal["HTTP"] = "HTTP"
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    path: Annotated[str, Field(pattern=r"^/")]
    json_body: dict | list | str | int | float | bool | None = None
    # variable name -> dotted path into the JSON response body
    save: dict[str, str] = Field(default_factory=dict)
    expect: Expect = Field(default_factory=Expect)


class ActionStep(_Strict):
    """A step the HTTP client cannot do; the caller supplies the implementation."""

    action: Literal["RESTART_DATABASE"]


Step = HttpStep | ActionStep  # told apart by `action`; extra fields are forbidden


class SuiteCase(_Strict):
    id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]*$")]
    description: str = ""
    source: Literal["STANDARD", "ORIGINAL", "AI"]
    effect: Literal["READ_ONLY", "WRITES_DATA"]
    external_side_effects: bool = False
    steps: list[Step] = Field(min_length=1)

    def has_value_assertion(self) -> bool:
        return any(s.expect.has_value_assertion() for s in self.steps if isinstance(s, HttpStep))


class Suite(_Strict):
    name: str
    cases: list[SuiteCase]


class CaseResult(_Strict):
    id: str
    status: Literal["PASSED", "FAILED", "SKIPPED"]
    reason: str | None = None  # a code, never response content


class SuiteResult(_Strict):
    suite_sha256: str
    results: list[CaseResult]

    @property
    def passed(self) -> bool:
        return all(r.status != "FAILED" for r in self.results) and any(
            r.status == "PASSED" for r in self.results
        )
