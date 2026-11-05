"""Application images that already exist, standing in for the Build step (D-12 is still open).

The Sandbox loop asks for an image per application. Until the real Build module is placed, an
operator names images that are already built and pushed, pinned by digest. A name that is not
digest-pinned is refused, so a moving tag can never be verified and then deployed as something else.
"""

import json
from typing import Any

from cmp_contracts.provisioning import Image
from pydantic import ValidationError

PLATFORMS = ["linux/amd64", "linux/arm64"]


class BuildUnavailable(Exception):
    """The loop needs an image this worker cannot supply. `code` is stable and safe to show."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class PrebuiltImages:
    def __init__(self, images: dict[str, str] | None = None) -> None:
        self._images: dict[str, Image] = {}
        for resource_id, uri in (images or {}).items():
            try:
                self._images[resource_id] = Image(uri=uri, platforms=PLATFORMS)
            except ValidationError:
                raise BuildUnavailable("PREBUILT_IMAGE_NOT_DIGEST_PINNED") from None

    @classmethod
    def from_json(cls, text: str | None) -> "PrebuiltImages":
        if not text:
            return cls()
        try:
            data = json.loads(text)
        except ValueError:
            raise BuildUnavailable("PREBUILT_IMAGES_INVALID") from None
        if not isinstance(data, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in data.items()
        ):
            raise BuildUnavailable("PREBUILT_IMAGES_INVALID")
        return cls(data)

    def __call__(self, resource_id: str, source: dict[str, Any]) -> Image:
        if not self._images:
            raise BuildUnavailable("BUILD_NOT_CONFIGURED")
        try:
            return self._images[resource_id]
        except KeyError:
            raise BuildUnavailable("PREBUILT_IMAGE_MISSING") from None
