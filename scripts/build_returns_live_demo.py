"""Build a local Returns live-mode config from uploaded stage images.

This registers uploaded files as trusted Returns inputs for demo workflows.
It does not fake observations; the Returns provider still has to inspect the
registered images and the deterministic rules still decide the evidence.
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.returns.core.returns_manager.domain import TenantContext
from agents.returns.core.returns_manager.storage import Store
from agents.returns.core.returns_manager.validation import parse_record, read_csv
from agents.returns.request_store import canonical, digest


def stage_images(unit_id: str) -> list[Path]:
    folder = ROOT / "data" / "input" / unit_id / "returns"
    if not folder.exists():
        raise SystemExit(f"Missing returns upload folder: {folder}")
    images = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    )
    if not images:
        raise SystemExit(f"No return images found in: {folder}")
    return images


def build(unit_id: str = "UNIT-0014", org_id: str = "org_demo_alpha") -> Path:
    rows = read_csv(ROOT / "data" / "sample" / "returns_sample.csv")
    matches = [(row, source) for row, source in rows if row["unit_id"] == unit_id and row["org_id"] == org_id]
    if len(matches) != 1:
        raise SystemExit(f"Expected exactly one returns sample row for {org_id}/{unit_id}, found {len(matches)}")

    row, source = matches[0]
    images = stage_images(unit_id)
    refs = [f"{unit_id}/returns/{image.name}" for image in images]

    trusted_row = dict(row)
    trusted_row["photo_refs"] = ";".join(refs)

    # Existing Returns mode requires explicit client scope. Keep it stable for local demo data.
    tenant = TenantContext(org_id, "client_alpha")
    capture = parse_record(trusted_row, tenant, source)

    output_root = ROOT / "data" / "input" / "returns_live"
    if output_root.exists():
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True)

    db_path = output_root / "source.sqlite3"
    with Store(db_path, tenant) as store:
        store.save_capture(capture)

    roles = ("returned_product", "returned_packaging")
    binding_images = []
    for index, (ref, image) in enumerate(zip(refs, images), start=1):
        content = image.read_bytes()
        binding_images.append({
            "ref": ref,
            "path": ref,
            "kind": "genuine",
            "sha256": hashlib.sha256(content).hexdigest(),
            "role": roles[(index - 1) % len(roles)],
            "image_id": f"RTN-IMG-{index:03d}",
            "evidence_id": f"RTN-EVD-{index:03d}",
        })

    config = {
        "mode": "existing",
        "tenants": [asdict(tenant)],
        "provider": {
            "name": "gemini",
            "settings": {
                "model": "gemini-3.8-flash",
                "timeout_seconds": 28,
                "free_tier_confirmed": True,
            },
        },
        "bindings": [{
            "organization_id": tenant.organization_id,
            "unit_id": capture.unit.unit_id,
            "record_id": capture.record_id,
            "database": "returns_live/source.sqlite3",
            "capture_sha256": digest(asdict(capture)),
            "inspection": "live",
            "images": binding_images,
        }],
    }

    config_path = ROOT / "data" / "input" / "returns_live_config.json"
    config_path.write_text(canonical(config) + "\n", encoding="utf-8")
    print(config_path)

    return config_path


if __name__ == "__main__":
    build(*(sys.argv[1:] or []))
