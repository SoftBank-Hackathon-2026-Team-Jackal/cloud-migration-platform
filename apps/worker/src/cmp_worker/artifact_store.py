"""Immutable local artifact store for analysis results (D-02, first cycle).

Same convention as the platform's `LocalArtifactStore` for Provisioning: the exact bytes live
under their SHA-256 file name and ids, kinds and versions are metadata only. S3 replaces the
directory later behind the same interface. Only references go into Workflow history.
"""

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Annotated, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class ArtifactError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ArtifactRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    size_bytes: int = Field(ge=0)


class ArtifactStore(Protocol):
    def put(self, content: bytes, *, ref_id: str, kind: str, version: str = "1") -> ArtifactRef: ...

    def get(self, ref: ArtifactRef) -> bytes: ...


class LocalArtifactStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)

    def _path(self, sha256: str) -> Path:
        if not _SHA256.fullmatch(sha256):
            raise ArtifactError("ARTIFACT_UNAVAILABLE")
        return self.root / sha256

    def put(self, content: bytes, *, ref_id: str, kind: str, version: str = "1") -> ArtifactRef:
        digest = hashlib.sha256(content).hexdigest()
        path = self._path(digest)
        if not path.exists():
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            handle, temporary = tempfile.mkstemp(dir=self.root, prefix=".tmp-")
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(content)
                os.chmod(temporary, 0o600)
                os.replace(temporary, path)  # atomic: readers never see partial content
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise
        return ArtifactRef(
            id=ref_id, version=version, kind=kind, sha256=digest, size_bytes=len(content)
        )

    def get(self, ref: ArtifactRef) -> bytes:
        try:
            content = self._path(ref.sha256).read_bytes()
        except OSError:
            raise ArtifactError("ARTIFACT_UNAVAILABLE") from None
        if hashlib.sha256(content).hexdigest() != ref.sha256:
            raise ArtifactError("ARTIFACT_DIGEST_MISMATCH")
        return content

    def put_json(self, value: Any, *, ref_id: str, kind: str, version: str = "1") -> ArtifactRef:
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        return self.put(data, ref_id=ref_id, kind=kind, version=version)

    def get_json(self, ref: ArtifactRef) -> Any:
        return json.loads(self.get(ref))
