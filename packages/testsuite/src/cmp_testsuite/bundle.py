"""Deterministic tar.gz bundle; its SHA-256 is the suite reference and the pin."""

import gzip
import hashlib
import io
import json
import tarfile

from cmp_testsuite.model import Suite


class SuiteError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def dump_bundle(suite: Suite) -> bytes:
    """Same suite -> same bytes: sorted keys, fixed mtime/owner, fixed gzip header."""
    files = {
        "meta.json": {"format": 1, "name": suite.name, "cases": sorted(c.id for c in suite.cases)},
        **{
            f"cases/{c.id}.json": c.model_dump(mode="json")
            for c in sorted(suite.cases, key=lambda c: c.id)
        },
    }
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(files):
            data = json.dumps(files[name], sort_keys=True, ensure_ascii=False, indent=1).encode()
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue(), mtime=0)


def load_bundle(data: bytes) -> Suite:
    from cmp_testsuite.model import SuiteCase

    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            members = {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}
        meta = json.loads(members["meta.json"])
        cases = [
            SuiteCase.model_validate_json(members[f"cases/{case_id}.json"])
            for case_id in meta["cases"]
        ]
        return Suite(name=meta["name"], cases=cases)
    except (OSError, KeyError, ValueError, tarfile.TarError):
        raise SuiteError("SUITE_UNREADABLE") from None


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
