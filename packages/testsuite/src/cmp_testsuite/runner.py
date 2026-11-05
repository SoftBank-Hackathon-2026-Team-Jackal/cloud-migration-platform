"""`run_suite`: run a pinned suite against any base URL (Sandbox now, Production later)."""

import json
import re
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from typing import Any, Literal

from cmp_testsuite.bundle import SuiteError, load_bundle, sha256_of
from cmp_testsuite.model import ActionStep, CaseResult, HttpStep, SuiteCase, SuiteResult

Actions = dict[str, Callable[[], None]]
_VAR = re.compile(r"\{\{([a-z_][a-z0-9_]*)\}\}")
_TIMEOUT = 15


class _Fail(Exception):
    pass


def _substitute(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, str):

        def fill(match: re.Match) -> str:
            if match.group(1) not in variables:
                raise _Fail(f"VAR_UNDEFINED {match.group(1)}")
            return variables[match.group(1)]

        return _VAR.sub(fill, value)
    if isinstance(value, list):
        return [_substitute(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, variables) for k, v in value.items()}
    return value


def _pick(document: Any, path: str) -> Any:
    current = document
    for part in path.removeprefix("$.").split("."):
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise _Fail(f"JSON_PATH_MISSING {path}")
    return current


def _call(
    base_url: str, step: HttpStep, headers: dict[str, str], variables: dict[str, str]
) -> tuple[int, bytes]:
    body = None
    request_headers = dict(headers)
    if step.json_body is not None:
        body = json.dumps(_substitute(step.json_body, variables)).encode()
        request_headers["Content-Type"] = "application/json"
    url = base_url.rstrip("/") + _substitute(step.path, variables)
    request = urllib.request.Request(url, data=body, method=step.method, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        raise _Fail("REQUEST_FAILED") from None


def _check(step: HttpStep, status: int, body: bytes, variables: dict[str, str]) -> None:
    expect = step.expect
    if expect.status is not None and status != expect.status:
        raise _Fail(f"STATUS_MISMATCH expected={expect.status} actual={status}")
    if expect.body_contains is not None and expect.body_contains not in body.decode(
        errors="ignore"
    ):
        raise _Fail("BODY_TEXT_MISSING")
    if not (expect.json_equals or expect.json_equals_var or step.save):
        return
    try:
        document = json.loads(body)
    except ValueError:
        raise _Fail("BODY_NOT_JSON") from None
    for path, wanted in expect.json_equals.items():
        if _pick(document, path) != _substitute(wanted, variables):
            raise _Fail(f"JSON_MISMATCH {path}")
    for path, name in expect.json_equals_var.items():
        if name not in variables:
            raise _Fail(f"VAR_UNDEFINED {name}")
        if str(_pick(document, path)) != variables[name]:
            raise _Fail(f"JSON_MISMATCH {path}")
    for name, path in step.save.items():
        variables[name] = str(_pick(document, path))


def run_case(
    case: SuiteCase,
    base_url: str,
    *,
    headers: dict[str, str] | None = None,
    actions: Actions | None = None,
    variables: dict[str, str] | None = None,
) -> CaseResult:
    variables = dict(variables or {})
    try:
        for step in case.steps:
            if isinstance(step, ActionStep):
                do = (actions or {}).get(step.action)
                if do is None:
                    raise _Fail(f"ACTION_UNAVAILABLE {step.action}")
                do()
                continue
            status, body = _call(base_url, step, headers or {}, variables)
            _check(step, status, body, variables)
    except _Fail as exc:
        return CaseResult(id=case.id, status="FAILED", reason=str(exc))
    return CaseResult(id=case.id, status="PASSED")


def run_suite(
    suite_ref: Any,
    base_url: str,
    auth_ref: str | None = None,
    include: Literal["READ_ONLY", "ALL"] = "ALL",
    *,
    store: Any,
    resolve_auth: Callable[[str], dict[str, str]] | None = None,
    actions: Actions | None = None,
    include_external: bool = False,
) -> SuiteResult:
    """Refuses to run when the stored bytes no longer match the pinned SHA-256.

    ALL runs every case without external side effects; READ_ONLY runs only read cases.
    Cases with external side effects run only when `include_external` is set explicitly.
    """
    if not base_url.startswith(("http://", "https://")):
        raise SuiteError("BASE_URL_INVALID")
    try:
        data = store.get(suite_ref)
    except Exception as exc:  # noqa: BLE001 - the store's own error type is not imported here
        code = str(getattr(exc, "code", ""))
        raise SuiteError(
            "SUITE_HASH_MISMATCH" if "MISMATCH" in code else "SUITE_UNAVAILABLE"
        ) from None
    if sha256_of(data) != suite_ref.sha256:
        raise SuiteError("SUITE_HASH_MISMATCH")
    suite = load_bundle(data)
    headers: dict[str, str] = {}
    if auth_ref is not None:
        if resolve_auth is None:
            raise SuiteError("AUTH_UNRESOLVED")
        headers = resolve_auth(auth_ref)
    variables = {"run_id": uuid.uuid4().hex[:12]}
    results = []
    for case in suite.cases:
        if case.external_side_effects and not include_external:
            results.append(CaseResult(id=case.id, status="SKIPPED", reason="EXTERNAL_SIDE_EFFECTS"))
        elif include == "READ_ONLY" and case.effect != "READ_ONLY":
            results.append(CaseResult(id=case.id, status="SKIPPED", reason="NOT_READ_ONLY"))
        else:
            results.append(
                run_case(case, base_url, headers=headers, actions=actions, variables=variables)
            )
    return SuiteResult(suite_sha256=suite_ref.sha256, results=results)
