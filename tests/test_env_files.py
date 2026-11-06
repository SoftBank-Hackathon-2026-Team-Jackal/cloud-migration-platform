"""ENV-01/ENV-02 seam: FastAPI TestClient + a temporary store directory, Temporal start faked.

Plaintext never leaving the store is checked by searching the stored bytes, every response body
and the captured log for the secret strings.
"""

import base64
import hashlib
import logging
import os
import stat
from datetime import UTC, datetime, timedelta

import pytest
from cmp_api.config import settings
from cmp_api.main import app
from cmp_api.routes import migrations as migration_routes
from cmp_api.routes.env_files import get_env_file_store
from cmp_contracts.provisioning import ContentRef
from cmp_providers.env_file_store import (
    EncryptedEnvFileStore,
    EnvFileError,
    load_key,
    parse_env,
)
from fastapi.testclient import TestClient

ADMIN = {"X-Enrollment-Admin-Key": "operator-only-test-key"}
SECRET = "s3cr3t-value-never-shown"
ENV = (
    "# comment\n"
    "export DB_PASSWORD='" + SECRET + "'\n"
    "API_TOKEN=" + SECRET + "-token\n"
    "\n"
    "PORT=8080\n"
    "PORT=9090\n"
).encode()
KEY = base64.urlsafe_b64encode(b"k" * 32).decode()


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 5, tzinfo=UTC)

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return EncryptedEnvFileStore(tmp_path / "env-files", load_key(KEY), clock=clock)


@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.setattr(settings, "agent_enrollment_admin_key", ADMIN["X-Enrollment-Admin-Key"])
    app.dependency_overrides[get_env_file_store] = lambda: store
    app.dependency_overrides[migration_routes.get_env_file_store_factory] = lambda: lambda: store
    yield TestClient(app)
    app.dependency_overrides.clear()


def _upload(client, body=ENV, path="/env-files"):
    return client.post(
        path, content=body, headers={**ADMIN, "Content-Type": "application/octet-stream"}
    )


def _stored_bytes(root) -> bytes:
    return b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file())


# -- ENV-01: upload, encryption, versions, delete, expiry ------------------------------------


def test_upload_returns_reference_and_names_only(client, store, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    response = _upload(client)
    assert response.status_code == 201
    body = response.json()
    ref = body["env_file_ref"]
    assert ref["version"] == "1"
    assert ref["sha256"] == hashlib.sha256(ENV).hexdigest()  # sha256 of the original bytes
    assert body["keys"] == ["API_TOKEN", "DB_PASSWORD", "PORT"]
    assert body["warnings"] == [{"code": "ENV_KEY_DUPLICATED", "key": "PORT"}]
    assert SECRET not in response.text and "9090" not in response.text
    assert SECRET not in caplog.text
    stored = _stored_bytes(tmp_path / "env-files")
    assert SECRET.encode() not in stored and b"DB_PASSWORD" not in stored  # ciphertext only
    for path in (tmp_path / "env-files").rglob("*"):
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == (0o700 if path.is_dir() else 0o600), path


def test_store_reads_names_and_values_through_protocol_methods(store):
    ref, _, _ = store.put(ENV)
    assert store.read_names(ref) == ["API_TOKEN", "DB_PASSWORD", "PORT"]
    assert store.read_values(ref, ["DB_PASSWORD", "PORT", "ABSENT"]) == {
        "DB_PASSWORD": SECRET,
        "PORT": "9090",  # last value wins
    }
    assert SECRET not in repr(store) and KEY not in repr(store)


def test_same_bytes_same_sha_and_new_version(client):
    first = _upload(client).json()["env_file_ref"]
    again = _upload(client, path=f"/env-files/{first['id']}").json()["env_file_ref"]
    assert again["id"] == first["id"]
    assert again["version"] == "2"
    assert again["sha256"] == first["sha256"]
    other = _upload(client, b"A=1\n", path=f"/env-files/{first['id']}").json()["env_file_ref"]
    assert other["version"] == "3" and other["sha256"] != first["sha256"]


def test_previous_version_stays_readable(store):
    v1, _, _ = store.put(b"A=1\n")
    v2, _, _ = store.put(b"A=2\n", file_id=v1.id)
    assert store.read_values(v1, ["A"]) == {"A": "1"}
    assert store.read_values(v2, ["A"]) == {"A": "2"}


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        (b"A=" + b"x" * (64 * 1024), 413, "ENV_FILE_TOO_LARGE"),
        (b"A=\xff\xfe\n", 422, "ENV_FILE_NOT_UTF8"),
    ],
)
def test_invalid_upload_is_refused_with_code_only(client, body, status, code):
    response = _upload(client, body)
    assert response.status_code == status
    assert response.json() == {"detail": {"code": code}}


def test_operator_key_is_required(client):
    for method, path in (
        ("post", "/env-files"),
        ("post", "/env-files/envf-" + "0" * 32),
        ("delete", "/env-files/envf-" + "0" * 32),
    ):
        response = getattr(client, method)(path, **({"content": ENV} if method == "post" else {}))
        assert response.status_code == 403
        wrong = getattr(client, method)(
            path,
            headers={"X-Enrollment-Admin-Key": "wrong"},
            **({"content": ENV} if method == "post" else {}),
        )
        assert wrong.status_code == 403


def test_operator_key_is_the_connection_and_agent_key_not_the_approval_key(client, monkeypatch):
    """Ticket 45 R7: the web BFF holds one operator key (`PLATFORM_ENROLLMENT_ADMIN_KEY` =
    `AGENT_ENROLLMENT_ADMIN_KEY`) for `/connections`, `/agent-enrollments` and `/env-files`."""
    from cmp_api.routes import agents, connections, env_files

    approval_key = "approval-only-test-key"
    monkeypatch.setattr(settings, "approval_admin_key", approval_key)
    assert _upload(client).status_code == 201
    refused = client.post(
        "/env-files",
        content=ENV,
        headers={"X-Approval-Admin-Key": approval_key, "Content-Type": "text/plain"},
    )
    assert refused.status_code == 403

    def guards(router) -> set:
        return {dep.dependency for dep in router.dependencies}

    assert agents.require_admin in guards(env_files.router)
    assert agents.require_admin in guards(connections.router)


def test_delete_then_read_is_expired(client, store):
    ref = ContentRef.model_validate(_upload(client).json()["env_file_ref"])
    assert client.delete(f"/env-files/{ref.id}", headers=ADMIN).status_code == 204
    assert client.delete(f"/env-files/{ref.id}", headers=ADMIN).status_code == 204  # idempotent
    for read in (store.read_names, lambda r: store.read_values(r, ["A"]), store.check):
        with pytest.raises(EnvFileError) as exc:
            read(ref)
        assert exc.value.code == "ENV_FILE_EXPIRED"
    assert _upload(client, path=f"/env-files/{ref.id}").status_code == 410


def test_unknown_reference_codes(store):
    ref, _, _ = store.put(b"A=1\n")
    cases = {
        "ENV_FILE_NOT_FOUND": [
            ref.model_copy(update={"id": "envf-" + "0" * 32}),
            ref.model_copy(update={"version": "9"}),
            ref.model_copy(update={"id": "../etc"}),
        ],
        "ENV_FILE_DIGEST_MISMATCH": [ref.model_copy(update={"sha256": "0" * 64})],
    }
    for code, refs in cases.items():
        for bad in refs:
            with pytest.raises(EnvFileError) as exc:
                store.read_names(bad)
            assert exc.value.code == code
            assert str(exc.value) == code


def test_tampered_ciphertext_is_digest_mismatch(store, tmp_path):
    ref, _, _ = store.put(b"A=1\n")
    blob = tmp_path / "env-files" / ref.id / "v1.bin"
    data = bytearray(blob.read_bytes())
    data[-1] ^= 1
    blob.write_bytes(bytes(data))
    with pytest.raises(EnvFileError) as exc:
        store.read_names(ref)
    assert exc.value.code == "ENV_FILE_DIGEST_MISMATCH"


def test_retention_seven_days_after_migration_end(store, clock):
    used, _, _ = store.put(b"A=1\n")
    store.bind_migration(used, "migration-1")
    unused, _, _ = store.put(b"B=1\n")
    ended = {"migration-1": None}
    assert store.bound_migrations() == ["migration-1"]

    clock.now += timedelta(days=30)
    assert store.purge_expired(ended.get) == [unused.id]  # unused upload: 7 days after upload
    assert store.read_names(used) == ["A"]  # migration still running: kept

    ended["migration-1"] = clock.now
    clock.now += timedelta(days=7) - timedelta(seconds=1)
    assert store.purge_expired(ended.get) == []
    clock.now += timedelta(seconds=1)
    assert store.purge_expired(ended.get) == [used.id]
    with pytest.raises(EnvFileError) as exc:
        store.read_values(used, ["A"])
    assert exc.value.code == "ENV_FILE_EXPIRED"


def test_key_must_be_32_bytes():
    for bad in (None, "", base64.urlsafe_b64encode(b"short").decode(), "%%%"):
        with pytest.raises(EnvFileError):
            load_key(bad)


def test_parse_rules_match_scanner_names():
    from cmp_scanners.envfile import parse_env_names

    text = 'export A=1\n B = "two words" \n#C=3\nnot a line\n_D=\nA=4\n'
    values, duplicates = parse_env(text)
    assert sorted(values) == parse_env_names(text)
    assert values == {"A": "4", "B": "two words", "_D": ""}
    assert duplicates == ["A"]


# -- ENV-02: start request validation --------------------------------------------------------


class FakeHandle:
    result_run_id = "run-1"


class FakeTemporal:
    def __init__(self):
        self.started = []

    async def start_workflow(self, name, data, **kwargs):
        self.started.append((name, data, kwargs))
        return FakeHandle()


@pytest.fixture
def temporal(monkeypatch):
    fake = FakeTemporal()

    async def get_client():
        return fake

    monkeypatch.setattr(migration_routes, "get_temporal_client", get_client)
    return fake


def test_start_without_reference_keeps_scaffold_input(client, temporal):
    response = client.post("/migrations", json={"project_id": "demo"})
    assert response.status_code == 202
    assert temporal.started[0][1] == "demo"


def test_start_with_reference_passes_reference_only(client, store, temporal):
    ref, _, _ = store.put(ENV)
    response = client.post(
        "/migrations", json={"project_id": "demo", "env_file_ref": ref.model_dump()}
    )
    assert response.status_code == 202
    data = temporal.started[0][1]
    assert data == {"project_id": "demo", "env_file_ref": ref.model_dump(mode="json")}
    assert SECRET not in repr(temporal.started)
    meta = store._meta(ref.id)
    assert meta["migration_id"] == response.json()["workflow_id"]


@pytest.mark.parametrize(
    ("update", "code"),
    [
        ({"id": "envf-" + "0" * 32}, "ENV_FILE_NOT_FOUND"),
        ({"version": "7"}, "ENV_FILE_NOT_FOUND"),
        ({"sha256": "0" * 64}, "ENV_FILE_DIGEST_MISMATCH"),
    ],
)
def test_start_with_bad_reference_is_422(client, store, temporal, update, code):
    ref, _, _ = store.put(ENV)
    bad = {**ref.model_dump(), **update}
    response = client.post("/migrations", json={"project_id": "demo", "env_file_ref": bad})
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": code}}
    assert SECRET not in response.text
    assert temporal.started == []


def test_start_with_deleted_reference_is_422_expired(client, store, temporal):
    ref, _, _ = store.put(ENV)
    store.delete(ref.id)
    response = client.post(
        "/migrations", json={"project_id": "demo", "env_file_ref": ref.model_dump()}
    )
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "ENV_FILE_EXPIRED"}}


def test_start_with_reference_but_no_store_is_503(temporal, monkeypatch):
    monkeypatch.setattr(settings, "env_file_store_dir", None)
    ref = {"id": "envf-" + "0" * 32, "version": "1", "sha256": "0" * 64}
    response = TestClient(app).post("/migrations", json={"project_id": "demo", "env_file_ref": ref})
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "ENV_FILE_STORE_UNCONFIGURED"}}


def test_store_satisfies_env03_protocol_and_error_type(tmp_path):
    """ENV-01's encrypted store is a structural `EnvFileStore` and raises ENV-03's error type."""
    import inspect

    from cmp_providers.env_file import EnvFileError as Env03Error
    from cmp_providers.env_file import EnvFileStore
    from cmp_providers.env_file_store import EncryptedEnvFileStore
    from cmp_providers.env_file_store import EnvFileError as StoreError

    assert StoreError is Env03Error
    for name in ("read_names", "read_values"):
        assert callable(getattr(EncryptedEnvFileStore, name))
        assert (
            list(inspect.signature(getattr(EnvFileStore, name)).parameters)[1:]
            == list(inspect.signature(getattr(EncryptedEnvFileStore, name)).parameters)[1:]
        )
