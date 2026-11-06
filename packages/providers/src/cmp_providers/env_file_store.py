"""Encrypted store for the user's uploaded `.env` (ENV-01, specs/env-file-ref).

One upload is kept once, encrypted, and is referred to only by `ContentRef{id, version, sha256}`
(`sha256` of the original bytes). Layout under the platform directory, every file 0600:

    <root>/<id>/meta.json     names of nothing secret: versions, sha256, timestamps, migration
    <root>/<id>/v<N>.bin      nonce(12) || AES-256-GCM(ciphertext || tag)

Cipher: AES-256-GCM from `cryptography` (AEAD; a fresh random 96-bit nonce per stored version and
the reference `id|version|sha256` as associated data, so a ciphertext cannot be swapped to another
reference). The 32-byte key comes from platform settings (`ENV_FILE_ENCRYPTION_KEY`, base64url),
never from the repository.

Retention (decision 2): a file is deleted 7 days after the migration that used it ended, or 7 days
after its last upload when no migration ever used it, or at once on an explicit delete. A read
after deletion is `ENV_FILE_EXPIRED`.

Nothing here logs, returns or puts a value in an exception; errors carry a stable `.code` only.
The read methods structurally satisfy ENV-03's `EnvFileStore` Protocol (`read_names`,
`read_values`). `EnvFileError` is ENV-03's `cmp_providers.env_file.EnvFileError`.
"""

import base64
import hashlib
import json
import os
import re
import secrets
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cmp_contracts.provisioning import ContentRef
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from cmp_providers.env_file import EnvFileError

MAX_ENV_FILE_BYTES = 64 * 1024
RETENTION = timedelta(days=7)
NONCE_BYTES = 12

_ID = re.compile(r"^envf-[a-f0-9]{32}$")
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def parse_env(text: str) -> tuple[dict[str, str], list[str]]:
    """`(values, duplicate keys)`. Same name rule as `cmp_scanners.envfile.parse_env_names`:
    lines that are not `[export ]NAME=...` (blank, comments, anything else) are skipped. A value
    loses surrounding whitespace and one pair of matching surrounding quotes; no escapes or inline
    comments are interpreted. A repeated key keeps its last value and is reported once."""
    values: dict[str, str] = {}
    duplicates: list[str] = []
    for line in text.splitlines():
        match = _LINE.match(line)
        if not match:
            continue
        name, raw = match.group(1), match.group(2).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
            raw = raw[1:-1]
        if name in values and name not in duplicates:
            duplicates.append(name)
        values[name] = raw
    return values, sorted(duplicates)


def decode_env(data: bytes) -> str:
    if len(data) > MAX_ENV_FILE_BYTES:
        raise EnvFileError("ENV_FILE_TOO_LARGE")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise EnvFileError("ENV_FILE_NOT_UTF8") from None


def load_key(encoded: str | None) -> bytes:
    """Decode the base64url 32-byte key from settings; refuse anything else."""
    if not encoded:
        raise EnvFileError("ENV_FILE_STORE_UNCONFIGURED")
    try:
        key = base64.urlsafe_b64decode(encoded.strip() + "=" * (-len(encoded.strip()) % 4))
    except ValueError:
        raise EnvFileError("ENV_FILE_KEY_INVALID") from None
    if len(key) != 32:
        raise EnvFileError("ENV_FILE_KEY_INVALID")
    return key


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _write_private(path: Path, data: bytes, *, exclusive: bool) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


class EncryptedEnvFileStore:
    def __init__(self, root: str | Path, key: bytes, *, clock: Callable[[], datetime] = _now):
        if len(key) != 32:
            raise EnvFileError("ENV_FILE_KEY_INVALID")
        self._root = Path(root)
        self._aead = AESGCM(key)
        self._clock = clock
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._root, 0o700)

    def __repr__(self) -> str:  # the key never shows
        return f"EncryptedEnvFileStore(root={str(self._root)!r})"

    # -- writing ---------------------------------------------------------------------------

    def put(
        self, data: bytes, file_id: str | None = None
    ) -> tuple[ContentRef, list[str], list[str]]:
        """Store a new file (or a new version of `file_id`). Returns `(ref, names, duplicates)`."""
        text = decode_env(data)
        values, duplicates = parse_env(text)
        sha = hashlib.sha256(data).hexdigest()
        if file_id is None:
            file_id = f"envf-{secrets.token_hex(16)}"
            directory = self._root / file_id
            directory.mkdir(mode=0o700)
            meta = {"id": file_id, "versions": {}, "migration_id": None, "ended_at": None}
        else:
            meta = self._meta(file_id)
            directory = self._root / file_id
        now = self._clock()
        version = max((int(v) for v in meta["versions"]), default=0) + 1
        while True:
            ref = ContentRef(id=file_id, version=str(version), sha256=sha)
            nonce = os.urandom(NONCE_BYTES)
            blob = nonce + self._aead.encrypt(nonce, data, self._aad(ref))
            try:
                _write_private(directory / f"v{version}.bin", blob, exclusive=True)
                break
            except FileExistsError:  # a concurrent upload took this version
                version += 1
        meta = self._meta(file_id) if meta["versions"] else meta
        meta["versions"][str(version)] = {"sha256": sha, "created_at": _iso(now)}
        meta["updated_at"] = _iso(now)
        self._save_meta(meta)
        return ref, sorted(values), duplicates

    def delete(self, file_id: str) -> None:
        """Erase every version now. Deleting an already deleted file is a no-op."""
        try:
            meta = self._meta(file_id)
        except EnvFileError as exc:
            if exc.code == "ENV_FILE_EXPIRED":
                return
            raise
        self._erase(meta, self._clock())

    def bind_migration(self, ref: ContentRef, migration_id: str) -> None:
        """Record which migration uses the file, so retention starts when that migration ends."""
        self.check(ref)
        meta = self._meta(ref.id)
        meta["migration_id"] = migration_id
        self._save_meta(meta)

    def bound_migrations(self) -> list[str]:
        """Migration IDs of stored (not deleted) files, for the purge job to look up."""
        found: set[str] = set()
        for directory in self._root.iterdir():
            try:
                meta = self._meta(directory.name)
            except EnvFileError:
                continue
            if meta.get("migration_id"):
                found.add(meta["migration_id"])
        return sorted(found)

    def purge_expired(
        self, ended_at: Callable[[str], datetime | None], now: datetime | None = None
    ) -> list[str]:
        """Delete files past retention; returns their IDs. `ended_at(migration_id)` is when that
        migration closed, or None while it still runs."""
        now = now or self._clock()
        purged: list[str] = []
        for directory in sorted(self._root.iterdir()):
            if not _ID.match(directory.name):
                continue
            try:
                meta = self._meta(directory.name)
            except EnvFileError:
                continue  # already deleted or unreadable: nothing to purge
            start: datetime | None
            if meta.get("migration_id"):
                start = ended_at(meta["migration_id"])
            else:
                start = datetime.fromisoformat(meta["updated_at"])
            if start is not None and now >= start + RETENTION:
                self._erase(meta, now)
                purged.append(directory.name)
        return purged

    # -- reading (ENV-03 EnvFileStore Protocol) ------------------------------------------

    def check(self, ref: ContentRef) -> None:
        """Raise unless `ref` names a stored version with the same sha256 (no decryption)."""
        meta = self._meta(ref.id)
        entry = meta["versions"].get(ref.version)
        if entry is None:
            raise EnvFileError("ENV_FILE_NOT_FOUND")
        if entry["sha256"] != ref.sha256:
            raise EnvFileError("ENV_FILE_DIGEST_MISMATCH")

    def latest(self, file_id: str) -> ContentRef:
        """The newest stored version of `file_id` (ticket 44: the web sends the id only)."""
        meta = self._meta(file_id)
        if not meta["versions"]:
            raise EnvFileError("ENV_FILE_NOT_FOUND")
        version = max(meta["versions"], key=int)
        return ContentRef(id=file_id, version=version, sha256=meta["versions"][version]["sha256"])

    def read_names(self, ref: ContentRef) -> list[str]:
        values, _ = parse_env(self._plaintext(ref))
        return sorted(values)

    def read_values(self, ref: ContentRef, keys: Sequence[str]) -> dict[str, str]:
        values, _ = parse_env(self._plaintext(ref))
        return {key: values[key] for key in keys if key in values}

    # -- internals ---------------------------------------------------------------------

    @staticmethod
    def _aad(ref: ContentRef) -> bytes:
        return f"{ref.id}|{ref.version}|{ref.sha256}".encode()

    def _plaintext(self, ref: ContentRef) -> str:
        self.check(ref)
        path = self._root / ref.id / f"v{ref.version}.bin"
        try:
            blob = path.read_bytes()
        except FileNotFoundError:
            raise EnvFileError("ENV_FILE_EXPIRED") from None
        try:
            data = self._aead.decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], self._aad(ref))
        except InvalidTag:
            raise EnvFileError("ENV_FILE_DIGEST_MISMATCH") from None
        if hashlib.sha256(data).hexdigest() != ref.sha256:
            raise EnvFileError("ENV_FILE_DIGEST_MISMATCH")
        return data.decode("utf-8")

    def _meta(self, file_id: str) -> dict:
        if not _ID.match(file_id):
            raise EnvFileError("ENV_FILE_NOT_FOUND")
        try:
            meta = json.loads((self._root / file_id / "meta.json").read_text())
        except FileNotFoundError:
            raise EnvFileError("ENV_FILE_NOT_FOUND") from None
        if meta.get("deleted_at"):
            raise EnvFileError("ENV_FILE_EXPIRED")
        return meta

    def _save_meta(self, meta: dict) -> None:
        directory = self._root / meta["id"]
        tmp = directory / "meta.json.tmp"
        _write_private(tmp, json.dumps(meta, sort_keys=True).encode(), exclusive=False)
        os.replace(tmp, directory / "meta.json")

    def _erase(self, meta: dict, now: datetime) -> None:
        directory = self._root / meta["id"]
        for blob in directory.glob("v*.bin"):
            blob.unlink(missing_ok=True)
        meta["deleted_at"] = _iso(now)
        self._save_meta(meta)
