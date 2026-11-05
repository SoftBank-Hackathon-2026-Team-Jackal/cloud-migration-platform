"""`.env` upload API for the web BFF (ENV-01, specs/env-file-ref).

Every route needs the operator key (`X-Enrollment-Admin-Key`) and is called server-side by the web
BFF only. The body is the raw `.env` bytes (at most 64 KiB, UTF-8). Responses carry the reference
and key names only; values never leave the encrypted store, and error bodies are a code only.
"""

from typing import Annotated

from cmp_contracts.provisioning import ContentRef
from cmp_providers.env_file_store import (
    MAX_ENV_FILE_BYTES,
    EncryptedEnvFileStore,
    EnvFileError,
)
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from cmp_api.env_store import configured_env_file_store
from cmp_api.routes.agents import require_admin
from cmp_api.routes.connections import InputHidingRoute

STATUS = {
    "ENV_FILE_NOT_FOUND": 404,
    "ENV_FILE_EXPIRED": 410,
    "ENV_FILE_TOO_LARGE": 413,
    "ENV_FILE_NOT_UTF8": 422,
    "ENV_FILE_DIGEST_MISMATCH": 422,
    "ENV_FILE_STORE_UNCONFIGURED": 503,
    "ENV_FILE_KEY_INVALID": 503,
}


def get_env_file_store() -> EncryptedEnvFileStore:
    try:
        return configured_env_file_store()
    except EnvFileError as exc:
        raise env_file_http_error(exc) from None


def env_file_http_error(exc: EnvFileError) -> HTTPException:
    return HTTPException(status_code=STATUS.get(exc.code, 422), detail={"code": exc.code})


StoreDep = Annotated[EncryptedEnvFileStore, Depends(get_env_file_store)]


class EnvFileWarning(BaseModel):
    code: str
    key: str


class EnvFileUploaded(BaseModel):
    env_file_ref: ContentRef
    keys: list[str]
    warnings: list[EnvFileWarning] = []


router = APIRouter(
    prefix="/env-files",
    tags=["env files"],
    dependencies=[Depends(require_admin)],
    route_class=InputHidingRoute,
)

_BODY = {
    "requestBody": {
        "required": True,
        "description": "Raw `.env` bytes, UTF-8, at most 64 KiB.",
        "content": {
            "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
            "text/plain": {"schema": {"type": "string"}},
        },
    }
}
_ERRORS = {
    403: {"description": "Operator key missing or wrong"},
    404: {"description": "`ENV_FILE_NOT_FOUND`"},
    410: {"description": "`ENV_FILE_EXPIRED` (deleted or past retention)"},
    413: {"description": "`ENV_FILE_TOO_LARGE`"},
    422: {"description": "`ENV_FILE_NOT_UTF8`"},
    503: {"description": "`ENV_FILE_STORE_UNCONFIGURED`"},
}


async def _read_body(request: Request) -> bytes:
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > MAX_ENV_FILE_BYTES:
            raise env_file_http_error(EnvFileError("ENV_FILE_TOO_LARGE"))
    return bytes(data)


def _uploaded(result) -> EnvFileUploaded:
    ref, keys, duplicates = result
    return EnvFileUploaded(
        env_file_ref=ref,
        keys=keys,
        warnings=[EnvFileWarning(code="ENV_KEY_DUPLICATED", key=k) for k in duplicates],
    )


@router.post("", status_code=201, response_model=EnvFileUploaded, openapi_extra=_BODY,
             responses=_ERRORS)  # fmt: skip
async def upload_env_file(request: Request, store: StoreDep) -> EnvFileUploaded:
    """Store a new `.env`; returns `env_file_ref` (version 1) and the key names."""
    data = await _read_body(request)
    try:
        return _uploaded(store.put(data))
    except EnvFileError as exc:
        raise env_file_http_error(exc) from None


@router.post("/{file_id}", status_code=201, response_model=EnvFileUploaded,
             openapi_extra=_BODY, responses=_ERRORS)  # fmt: skip
async def upload_env_file_version(
    file_id: str, request: Request, store: StoreDep
) -> EnvFileUploaded:
    """Store a new version of an existing `.env`; earlier versions stay until retention."""
    data = await _read_body(request)
    try:
        return _uploaded(store.put(data, file_id=file_id))
    except EnvFileError as exc:
        raise env_file_http_error(exc) from None


@router.delete("/{file_id}", status_code=204, responses={k: _ERRORS[k] for k in (403, 404, 503)})
def delete_env_file(file_id: str, store: StoreDep) -> Response:
    """Delete every version now; later reads of any of its references are `ENV_FILE_EXPIRED`."""
    try:
        store.delete(file_id)
    except EnvFileError as exc:
        raise env_file_http_error(exc) from None
    return Response(status_code=204)
