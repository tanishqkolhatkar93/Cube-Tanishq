"""Reference photo library for the Returns comparison.

A reference set is the catalogue baseline for one SKU in one organisation: a few photos (front, back,
identification, accessories, condition...) that a returned unit is compared against by a vision model.

  list(org_id, sku)  -> metadata only (angle, ref, sha256); cheap, used for the request fingerprint
  load(meta)         -> the image bytes, re-verified against the recorded SHA-256

SHA-256 here is an INTEGRITY check (the bytes judged are the bytes registered). It is never used to decide
whether two images look alike; that is the vision comparison's job (see docs/product/returns-image-verification.md).

Backends: DirReferenceStore (local folder, demos and tests) and SupabaseReferenceStore (Postgres + Storage).
Everything is scoped by org_id; an unknown, unsafe or foreign SKU simply has no references.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import httpx

MAX_REFERENCE_IMAGES = 8
MAX_IMAGE_BYTES = 10_000_000
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ANGLE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_SUFFIXES = {".jpg", ".jpeg", ".png"}


class ReferenceStoreError(RuntimeError):
    """The library could not be read, or an image no longer matches its registered hash. No secrets inside."""


@dataclass(frozen=True)
class ReferenceMeta:
    org_id: str
    sku: str
    angle: str
    ref: str          # stable label used in evidence: "reference:org/<org>/reference/<sku>/<angle>/<file>"
    sha256: str
    locator: str      # backend-specific (relative file path, or storage object path)


class ReferenceStore(Protocol):
    def list(self, org_id: str, sku: str) -> tuple[ReferenceMeta, ...]: ...
    def load(self, meta: ReferenceMeta) -> bytes: ...


def _safe(org_id: str, sku: str) -> bool:
    return isinstance(org_id, str) and isinstance(sku, str) and bool(_ID.fullmatch(org_id)) and bool(_ID.fullmatch(sku))


def _verify(meta: ReferenceMeta, content: bytes) -> bytes:
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise ReferenceStoreError("reference_image_unreadable")
    if hashlib.sha256(content).hexdigest() != meta.sha256:
        raise ReferenceStoreError("reference_image_changed")
    return content


def _label(org_id: str, sku: str, angle: str, name: str) -> str:
    return f"reference:org/{org_id}/reference/{sku}/{angle}/{name}"


class DirReferenceStore:
    """root/<org_id>/<sku>/<angle>/<image files>. Read-only; symlinks and non-images are ignored."""

    def __init__(self, root):
        self.root = Path(root).resolve()

    def list(self, org_id, sku):
        if not _safe(org_id, sku):
            return ()
        base = (self.root / org_id / sku).resolve()
        if not base.is_dir() or not base.is_relative_to(self.root):
            return ()
        metas = []
        for angle_dir in sorted(p for p in base.iterdir() if p.is_dir() and not p.is_symlink() and _ANGLE.fullmatch(p.name)):
            for file in sorted(p for p in angle_dir.iterdir() if p.is_file() and not p.is_symlink() and p.suffix.lower() in _SUFFIXES):
                if file.stat().st_size > MAX_IMAGE_BYTES:
                    continue
                digest = hashlib.sha256(file.read_bytes()).hexdigest()
                metas.append(ReferenceMeta(org_id, sku, angle_dir.name, _label(org_id, sku, angle_dir.name, file.name), digest,
                                           f"{org_id}/{sku}/{angle_dir.name}/{file.name}"))
        return tuple(metas[:MAX_REFERENCE_IMAGES])

    def load(self, meta):
        path = (self.root / meta.locator).resolve()
        if not path.is_file() or path.is_symlink() or not path.is_relative_to(self.root):
            raise ReferenceStoreError("reference_image_unreadable")
        try:
            return _verify(meta, path.read_bytes())
        except OSError:
            raise ReferenceStoreError("reference_image_unreadable") from None


class SupabaseReferenceStore:
    """Metadata in `reference_images`, bytes in the private `cube-inputs` Storage bucket. Server-side key only."""

    def __init__(self, url: str, service_key: str, *, bucket: str = "cube-inputs", client: httpx.Client | None = None,
                 timeout: float = 15.0):
        from orchestration.supabase_store import _validated_url   # same URL policy as the workflow store
        if not isinstance(service_key, str) or len(service_key) < 20 or any(ord(c) < 33 or ord(c) > 126 for c in service_key):
            raise ValueError("a Supabase service key is required")
        base = _validated_url(url)
        self._rest, self._storage, self.bucket = base + "/rest/v1", base + "/storage/v1/object", bucket
        self._headers = {"apikey": service_key, "Authorization": f"Bearer {service_key}"}
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=False)

    def _send(self, method, url, **kw):
        try:
            response = self._client.request(method, url, headers={**self._headers, **kw.pop("headers", {})}, **kw)
        except httpx.HTTPError:
            raise ReferenceStoreError("reference_store_unreachable") from None
        return response

    def list(self, org_id, sku):
        if not _safe(org_id, sku):
            return ()
        response = self._send("GET", f"{self._rest}/reference_images", params={
            "org_id": f"eq.{org_id}", "sku": f"eq.{sku}", "select": "angle,storage_path,sha256", "order": "angle"})
        if response.status_code != 200:
            raise ReferenceStoreError("reference_store_error")
        prefix = f"org/{org_id}/reference/{sku}/"
        metas = []
        for row in response.json():
            path, angle, sha = row.get("storage_path"), row.get("angle"), row.get("sha256")
            # A row pointing outside this org's/SKU's folder is ignored, never followed.
            if (not isinstance(path, str) or not path.startswith(prefix) or ".." in path
                    or not isinstance(angle, str) or not _ANGLE.fullmatch(angle)
                    or not isinstance(sha, str) or not _SHA.fullmatch(sha)):
                continue
            metas.append(ReferenceMeta(org_id, sku, angle, f"reference:{path}", sha, path))
        return tuple(metas[:MAX_REFERENCE_IMAGES])

    def load(self, meta):
        response = self._send("GET", f"{self._storage}/{self.bucket}/{quote(meta.locator, safe='/')}")
        if response.status_code != 200:
            raise ReferenceStoreError("reference_image_unreadable")
        return _verify(meta, response.content)

    # --- administration (seed script / future admin UI) ------------------------------------------

    def add_product(self, org_id: str, sku: str, name: str, expected_components: list[str] | None = None) -> None:
        if not _safe(org_id, sku) or not name.strip():
            raise ValueError("valid org, SKU and product name required")
        response = self._send("POST", f"{self._rest}/reference_products", params={"on_conflict": "org_id,sku"},
                              headers={"Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"},
                              json=[{"org_id": org_id, "sku": sku, "name": name.strip(), "expected_components": expected_components or []}])
        if response.status_code >= 300:
            raise ReferenceStoreError("reference_store_error")

    def add_image(self, org_id: str, sku: str, angle: str, filename: str, content: bytes, uploaded_by: str) -> ReferenceMeta:
        if not _safe(org_id, sku) or not _ANGLE.fullmatch(angle) or not _ID.fullmatch(filename) or Path(filename).suffix.lower() not in _SUFFIXES:
            raise ValueError("valid org, SKU, angle and image filename required")
        if not content or len(content) > MAX_IMAGE_BYTES:
            raise ValueError("image must be 1 byte to 10 MB")
        path = f"org/{org_id}/reference/{sku}/{angle}/{filename}"
        sha = hashlib.sha256(content).hexdigest()
        upload = self._send("POST", f"{self._storage}/{self.bucket}/{quote(path, safe='/')}", content=content,
                            headers={"Content-Type": "application/octet-stream", "x-upsert": "false"})
        if upload.status_code >= 300:
            raise ReferenceStoreError("reference_upload_failed")
        row = self._send("POST", f"{self._rest}/reference_images", params={"on_conflict": "storage_path"},
                         headers={"Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"},
                         json=[{"org_id": org_id, "sku": sku, "angle": angle, "storage_path": path, "sha256": sha, "uploaded_by": uploaded_by}])
        if row.status_code >= 300:
            raise ReferenceStoreError("reference_register_failed")
        return ReferenceMeta(org_id, sku, angle, f"reference:{path}", sha, path)
