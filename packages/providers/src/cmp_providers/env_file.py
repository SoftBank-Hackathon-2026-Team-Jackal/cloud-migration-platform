"""Shared `.env` reference functions (ENV-005, ENV-007, ENV-008; ticket ENV-03, #147).

- `EnvFileStore`: read interface the ENV-01 encrypted store implements structurally.
- `resolve_env_names`: key names only (analysis). Never reads values.
- `render_env`: the only function that reads values. Sandbox and Provisioning call it with the same
  `EnvPlan` and `env_file_ref`; only `db_outputs` differs.

Secret layout (ENV-03 decision D3):
- app env: `<app release>-env`, one key per PASS variable (created by the applying actor)
- DB connection: `<db release>-conn` with keys `host`, `port`, `name`, `username`
  (built from the DB outputs with `connection_secret_data`; ENV-04/ENV-06 create it)
- DB password: the existing `password_secret_ref` (Sandbox: `<db release>-auth` / `password`)
- DB URL: the existing `<db release>-url` / `url` (ticket 19)
"""

import hashlib
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from cmp_contracts.env_plan import (
    EnvDecision,
    EnvPlan,
    EnvVarRef,
    RenderedEnv,
    RenderedSecret,
)
from cmp_contracts.provisioning import ContentRef, Contract
from cmp_contracts.shared_deployment_v2 import DatabaseOutputsV2, SecretRefV2
from cmp_scanners.envfile import _NAME, parse_env_names
from pydantic import SecretStr

from cmp_providers.database_url import URL_SECRET_KEY, connection_url_secret_name

ENV_FILE_ERROR_CODES = frozenset(
    {"ENV_FILE_NOT_FOUND", "ENV_FILE_DIGEST_MISMATCH", "ENV_FILE_EXPIRED"}
)
CONNECTION_SECRET_KEYS = ("host", "port", "name", "username")
PASSWORD_SECRET_KEY = "password"


class EnvFileError(Exception):
    """Structured failure; the message is the code only, never a value."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@runtime_checkable
class EnvFileStore(Protocol):
    """Read side of the `.env` store. Raises `EnvFileError` with `ENV_FILE_NOT_FOUND`,
    `ENV_FILE_DIGEST_MISMATCH` or `ENV_FILE_EXPIRED`."""

    def read_names(self, ref: ContentRef) -> list[str]: ...

    def read_values(self, ref: ContentRef, keys: Sequence[str]) -> dict[str, str]: ...


def parse_env_values(text: str) -> dict[str, str]:
    """Same line rules as `cmp_scanners.envfile.parse_env_names` (comments, `export`, blank lines);
    surrounding matching quotes are stripped and the last duplicate wins."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _NAME.match(line)
        if not match:
            continue
        value = line[match.end() :].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[match.group(1)] = value
    return values


class InMemoryEnvFileStore:
    """Test double for `EnvFileStore`. Counts `read_values` calls; `repr` shows no content."""

    def __init__(self) -> None:
        self._files: dict[tuple[str, str], tuple[str, str]] = {}
        self._versions: dict[str, int] = {}
        self._expired: set[tuple[str, str]] = set()
        self.read_values_calls = 0

    def __repr__(self) -> str:
        return f"InMemoryEnvFileStore(files={len(self._files)})"

    def put(self, content: bytes, *, file_id: str | None = None) -> ContentRef:
        file_id = file_id or f"env-{len(self._versions) + 1}"
        version = self._versions.get(file_id, 0) + 1
        self._versions[file_id] = version
        self._files[(file_id, str(version))] = (
            hashlib.sha256(content).hexdigest(),
            content.decode("utf-8"),
        )
        return ContentRef(
            id=file_id, version=str(version), sha256=self._files[(file_id, str(version))][0]
        )

    def expire(self, ref: ContentRef) -> None:
        self._expired.add((ref.id, ref.version))

    def _text(self, ref: ContentRef) -> str:
        key = (ref.id, ref.version)
        if key not in self._files:
            raise EnvFileError("ENV_FILE_NOT_FOUND")
        if key in self._expired:
            raise EnvFileError("ENV_FILE_EXPIRED")
        sha256, text = self._files[key]
        if sha256 != ref.sha256:
            raise EnvFileError("ENV_FILE_DIGEST_MISMATCH")
        return text

    def read_names(self, ref: ContentRef) -> list[str]:
        return parse_env_names(self._text(ref))

    def read_values(self, ref: ContentRef, keys: Sequence[str]) -> dict[str, str]:
        self.read_values_calls += 1
        values = parse_env_values(self._text(ref))
        return {k: values[k] for k in keys if k in values}


def resolve_env_names(store: EnvFileStore, env_file_ref: ContentRef) -> list[str]:
    """Key names only; values never leave the store (analysis needs no value access)."""
    return store.read_names(env_file_ref)


# ------------------------------------------------------------------- DB output Secrets


def app_env_secret_name(app_release: str) -> str:
    return f"{app_release}-env"


def connection_secret_name(database_release: str) -> str:
    """DB release names are at most 53 characters, so the Secret name stays within 63."""
    return f"{database_release}-conn"


def connection_secret_data(outputs: DatabaseOutputsV2) -> dict[str, str]:
    """Data of the `<db release>-conn` Secret the applying actor creates (ENV-04, ENV-06)."""
    return {
        "host": outputs.host,
        "port": str(outputs.port),
        "name": outputs.name,
        "username": outputs.username,
    }


class DbOutputRefs(Contract):
    """Where each REPLACED binding points: one Secret key per binding, never a plain value."""

    host: SecretRefV2
    port: SecretRefV2
    name: SecretRefV2
    username: SecretRefV2
    password_secret_ref: SecretRefV2
    connection_url_secret_ref: SecretRefV2

    @classmethod
    def from_outputs(cls, outputs: DatabaseOutputsV2, *, database_release: str) -> "DbOutputRefs":
        """Provisioning: password and URL refs come from the shared deployment outputs."""
        return cls(
            **_connection_refs(database_release),
            password_secret_ref=outputs.password_secret_ref,
            connection_url_secret_ref=outputs.connection_url_secret_ref,
        )


def _connection_refs(database_release: str) -> dict[str, SecretRefV2]:
    name = connection_secret_name(database_release)
    return {key: SecretRefV2(name=name, key=key) for key in CONNECTION_SECRET_KEYS}


def db_output_refs(database_release: str) -> DbOutputRefs:
    """Standard names: `-conn`, `-auth`/`password` (Sandbox), `-url`/`url` (ticket 19)."""
    return DbOutputRefs(
        **_connection_refs(database_release),
        password_secret_ref=SecretRefV2(name=f"{database_release}-auth", key=PASSWORD_SECRET_KEY),
        connection_url_secret_ref=SecretRefV2(
            name=connection_url_secret_name(database_release), key=URL_SECRET_KEY
        ),
    )


# -------------------------------------------------------------------------- render


def _blocked(decisions: list[EnvDecision]) -> RenderedEnv:
    return RenderedEnv(env=[], secrets=[], decisions=decisions)


def _plan_decisions(env_plan: EnvPlan, db_outputs: DbOutputRefs | None) -> list[EnvDecision]:
    decisions = []
    for entry in sorted(env_plan.entries, key=lambda e: e.key):
        if entry.disposition == "UNREPRESENTABLE":
            decisions.append(EnvDecision(key=entry.key, code="ENV_UNREPRESENTABLE"))
        elif entry.disposition == "MISSING":
            decisions.append(EnvDecision(key=entry.key, code="ENV_MISSING"))
        elif entry.disposition == "REPLACED" and db_outputs is None:
            decisions.append(EnvDecision(key=entry.key, code="ENV_DB_OUTPUT_MISSING"))
    return decisions


def _pass_keys(env_plan: EnvPlan) -> list[str]:
    return sorted(e.key for e in env_plan.entries if e.disposition == "PASS")


def _env_refs(
    env_plan: EnvPlan, db_outputs: DbOutputRefs | None, app_release: str
) -> list[EnvVarRef]:
    secret_name = app_env_secret_name(app_release)
    env: list[EnvVarRef] = []
    for entry in sorted(env_plan.entries, key=lambda e: e.key):
        if entry.disposition == "PASS":
            ref = SecretRefV2(name=secret_name, key=entry.key)
        elif entry.disposition == "REPLACED":
            ref = getattr(db_outputs, entry.binding)
        else:
            continue
        env.append(EnvVarRef(name=entry.key, secret_ref=ref))
    return env


def render_env(
    store: EnvFileStore,
    env_plan: EnvPlan,
    env_file_ref: ContentRef,
    db_outputs: DbOutputRefs | None,
    *,
    app_release: str,
) -> RenderedEnv:
    """EnvPlan + `.env` reference → container env (`secretKeyRef` only) and the app env Secret.

    - PASS: value into `<app release>-env`, env refers to it
    - REPLACED: env refers to the `db_outputs` Secret key for the binding
    - EXCLUDED: omitted
    - UNREPRESENTABLE, MISSING, PASS without a value, REPLACED without `db_outputs`: returned as
      `decisions` with empty `env` and `secrets` (decision D1: blocked before approval)

    Output is sorted by key, so the same inputs give the same bytes.
    """
    if env_plan.env_file_ref != env_file_ref:
        raise EnvFileError("ENV_PLAN_REF_MISMATCH")
    store.read_names(env_file_ref)  # existence, digest and expiry; names only
    decisions = _plan_decisions(env_plan, db_outputs)
    if decisions:
        return _blocked(decisions)

    pass_keys = _pass_keys(env_plan)
    values = store.read_values(env_file_ref, pass_keys) if pass_keys else {}
    absent = [k for k in pass_keys if k not in values]
    if absent:
        return _blocked([EnvDecision(key=k, code="ENV_VALUE_MISSING") for k in absent])

    secret_name = app_env_secret_name(app_release)
    secrets = (
        [RenderedSecret(name=secret_name, data={k: SecretStr(values[k]) for k in pass_keys})]
        if pass_keys
        else []
    )
    return RenderedEnv(
        env=_env_refs(env_plan, db_outputs, app_release), secrets=secrets, decisions=[]
    )


def preview_env(
    store: EnvFileStore,
    env_plan: EnvPlan,
    env_file_ref: ContentRef,
    db_outputs: DbOutputRefs | None,
    *,
    app_release: str,
) -> RenderedEnv:
    """Plan-time twin of `render_env` (ENV-04): same `env` and `decisions`, but reads key names
    only, so `secrets` is always empty. `ENV_VALUE_MISSING` is decided from the declared names
    (a declared key always has a value, possibly empty)."""
    if env_plan.env_file_ref != env_file_ref:
        raise EnvFileError("ENV_PLAN_REF_MISMATCH")
    names = set(store.read_names(env_file_ref))
    decisions = _plan_decisions(env_plan, db_outputs)
    if decisions:
        return _blocked(decisions)
    absent = [k for k in _pass_keys(env_plan) if k not in names]
    if absent:
        return _blocked([EnvDecision(key=k, code="ENV_VALUE_MISSING") for k in absent])
    return RenderedEnv(env=_env_refs(env_plan, db_outputs, app_release), secrets=[], decisions=[])


def database_outputs(
    *, release: str, namespace: str, name: str, username: str, password_secret_ref: SecretRefV2
) -> DatabaseOutputsV2:
    """Production DB outputs from approved, non-secret Values (ENV-04): the Service host, the
    password Secret and the ticket 19 URL Secret. Same shape as `render_shared_deployment_v2`."""
    return DatabaseOutputsV2(
        host=f"{release}.{namespace}.svc",
        port=5432,
        name=name,
        username=username,
        password_secret_ref=password_secret_ref,
        connection_url_secret_ref=SecretRefV2(
            name=connection_url_secret_name(release), key=URL_SECRET_KEY
        ),
    )


def deployment_secrets(
    rendered: RenderedEnv, outputs: DatabaseOutputsV2, *, database_release: str
) -> list[RenderedSecret]:
    """Secrets the executing actor applies for one app (ENV-04): `<app release>-env` from
    `render_env` and `<db release>-conn` when the env refers to it. `-auth`/`-url` are made by
    the existing DB password and ticket 19 steps, not here."""
    conn = connection_secret_name(database_release)
    secrets = list(rendered.secrets)
    if any(item.secret_ref.name == conn for item in rendered.env):
        data = connection_secret_data(outputs)
        secrets.append(RenderedSecret(name=conn, data={k: SecretStr(v) for k, v in data.items()}))
    return sorted(secrets, key=lambda s: s.name)
