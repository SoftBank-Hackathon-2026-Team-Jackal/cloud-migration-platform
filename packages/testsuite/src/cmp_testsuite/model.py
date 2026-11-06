"""Test suite definition: HTTP scenarios plus the metadata the runner and validator need.

Names avoid a `Test` prefix so pytest never collects them.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator


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
    # A path, or a variable such as `{{health_path}}` that the runner fills in from the target.
    path: Annotated[str, Field(pattern=r"^(/|\{\{[a-z_]+\}\})")]
    json_body: dict | list | str | int | float | bool | None = None
    # variable name -> dotted path into the JSON response body
    save: dict[str, str] = Field(default_factory=dict)
    expect: Expect = Field(default_factory=Expect)


class ActionStep(_Strict):
    """A step the HTTP client cannot do; the caller supplies the implementation."""

    action: Literal["RESTART_DATABASE"]
    # After the action the next HTTP step is sent again for this many seconds while the app answers
    # 5xx or does not answer (an app that is told to "retry with the same ID" while its database
    # comes back). Any other answer, such as a 201 for lost data, is judged at once.
    recover_seconds: int = Field(default=0, ge=0, le=300)


class ProbeStep(_Strict):
    """Try to open a TCP connection from a short-lived Pod; the caller supplies the implementation.

    `ALLOWED_CLIENT` is a Pod with the identity of `source_resource_id` (it should reach the
    target); `OUTSIDER` has none of the identities the network intent lists (it should not). Which
    resources and ports to probe comes from the requirements' network intent, never from here.
    """

    action: Literal["NETWORK_PROBE"] = "NETWORK_PROBE"
    role: Literal["ALLOWED_CLIENT", "OUTSIDER"]
    source_resource_id: str | None = None
    target_resource_id: str
    port: int = Field(ge=1, le=65535)
    expect: Literal["CONNECTS", "BLOCKED"]

    @model_validator(mode="after")
    def _source_only_for_an_allowed_client(self):
        if (self.role == "ALLOWED_CLIENT") != (self.source_resource_id is not None):
            raise ValueError("An allowed client names its source; an outsider has none")
        return self


Step = HttpStep | ActionStep | ProbeStep  # told apart by `action`; extra fields are forbidden


class Evidence(_Strict):
    """A place in the fetched source that a test was written from (ticket 27-B)."""

    file: str = Field(min_length=1, max_length=512)
    line: int = Field(ge=1)


class SuiteCase(_Strict):
    id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]*$")]
    description: str = ""
    source: Literal["STANDARD", "ORIGINAL", "AI"]
    effect: Literal["READ_ONLY", "WRITES_DATA"]
    external_side_effects: bool = False
    covers: list[str] = Field(default_factory=list)  # catalog capabilities this test exercises
    # Where in the source an AI test comes from. `evidence` and `basis` are left out of the bytes
    # when unset, so suites pinned before these fields existed keep their SHA-256.
    evidence: list[Evidence] = Field(default_factory=list)
    # How the expected values came to be. FREE: written by someone (a model, a person). RECORDED:
    # taken from what the app answered when the platform asked it (ticket 27-B); only the recorder
    # sets this, a model's submission may not.
    basis: Literal["FREE", "RECORDED"] = "FREE"
    steps: list[Step] = Field(min_length=1)

    @model_serializer(mode="wrap")
    def _leave_out_what_is_unset(self, handler):
        data = handler(self)
        if not data.get("evidence"):
            data.pop("evidence", None)
        if data.get("basis") == "FREE":
            data.pop("basis", None)
        return data

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
