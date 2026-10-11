"""Returns adapter with reference-image comparison enabled: full Evidence Record, replay, tenancy, failure."""
import io
from dataclasses import asdict

import pytest
from PIL import Image

from agents.returns.adapter import Adapter
from agents.returns.reference_compare import ScriptedComparer
from agents.returns.reference_store import DirReferenceStore
from agents.returns.request_store import Rejected
from shared.utils.hashing import verify
from shared.utils.schema import errors
from tests.integration.test_returns_adapter import MockReal, existing, request_for  # noqa: F401  (pytest fixture + helpers)

REQUEST = request_for()


def _jpeg(color):
    out = io.BytesIO()
    Image.new("RGB", (80, 60), color).save(out, "JPEG")
    return out.getvalue()


def _library(tmp_path, org, sku, colors=((200, 0, 0), (0, 200, 0))):
    for angle, color in zip(("front", "back"), colors):
        folder = tmp_path / "refs" / org / sku / angle
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{angle}.jpg").write_bytes(_jpeg(color))
    return DirReferenceStore(tmp_path / "refs")


def _finding(state, ret=("RET-1",), ref=("REF-1",), **extra):
    return {"state": state, "reason": "visible in the photos", "returned_ids": list(ret), "reference_ids": list(ref), "confidence": 0.9, **extra}


GOOD = {"identity": _finding("same"), "completeness": _finding("complete", ref=()), "damage": _finding("none", ref=()),
        "condition": _finding("as_reference")}


def _adapter(existing, store=None, comparer=None):
    config, capture, root = existing
    return Adapter(config, root, root / "state", provider=MockReal(), reference_store=store, reference_comparer=comparer), capture, root


def test_disabled_by_default_leaves_the_legacy_record_untouched(existing):
    adapter, _, _ = _adapter(existing)
    out = adapter.handle(REQUEST)
    keys = [c["check_key"] for c in out["evidence"]["checks"]]
    assert keys == ["identity_match", "completeness", "condition"]
    assert "reference_comparison" not in out["evidence"]["payload"]
    assert out["evidence"]["decision"]["outcome"] == "pending_review"


def test_enabled_produces_a_valid_sealed_record_with_the_four_reference_checks(existing, tmp_path):
    config, capture, _ = existing
    store = _library(tmp_path, capture.tenant.organization_id, capture.order.ordered_sku)
    comparer = ScriptedComparer(GOOD)
    adapter, _, _ = _adapter(existing, store, comparer)
    out = adapter.handle(REQUEST)

    assert not errors("agent-output", out) and not errors("evidence", out["evidence"]) and verify(out["evidence"])
    record = out["evidence"]
    assert [c["check_key"] for c in record["checks"]] == ["reference_identity", "reference_completeness", "reference_damage", "reference_condition"]
    assert {c["verdict"] for c in record["checks"]} == {"PASS"}
    assert out["verdict"] == "PASS" and record["decision"]["outcome"] == "restock" and record["decision"]["needs_human"] is False
    assert out["next_step_recommendation"]["action"] != "review"

    # the exact reference photos judged are recorded, content-addressed, alongside the returned photos
    refs = [i for i in record["inputs"] if i["ref"].startswith("reference:")]
    assert len(refs) == 2 and all(i["kind"] == "image" and len(i["sha256"]) == 64 for i in refs)
    cited = {r for c in record["checks"] for r in c.get("evidence_refs", [])}
    assert cited <= {i["ref"] for i in record["inputs"]}, "every cited ref must be a recorded input"

    assert record["payload"]["reference_comparison"]["disposition"] == "restock"
    assert "assessment" in record["payload"], "legacy engine assessment stays available for audit"
    assert out["model"]["calls"] == 2, "one comparison call plus the existing observation call, both counted"
    assert len(comparer.requests) == 1


def test_replay_is_idempotent_but_changed_references_conflict(existing, tmp_path):
    config, capture, _ = existing
    org, sku = capture.tenant.organization_id, capture.order.ordered_sku
    adapter, _, _ = _adapter(existing, _library(tmp_path, org, sku), ScriptedComparer(GOOD))
    first = adapter.handle(REQUEST)
    assert adapter.handle(REQUEST) == first                                 # replay: same record, no second model call
    changed, _, _ = _adapter(existing, _library(tmp_path, org, sku, colors=((1, 2, 3), (4, 5, 6))), ScriptedComparer(GOOD))
    with pytest.raises(Rejected) as raised:
        changed.handle(REQUEST)                                             # same request ID, different references
    assert raised.value.code == "request_content_conflict"


def test_another_tenants_references_are_never_used(existing, tmp_path):
    config, capture, _ = existing
    org = capture.tenant.organization_id
    other = "org_demo_alpha" if org == "org_demo_bravo" else "org_demo_bravo"
    store = _library(tmp_path, other, capture.order.ordered_sku)             # only ANOTHER org registered references
    comparer = ScriptedComparer(GOOD)
    adapter, _, _ = _adapter(existing, store, comparer)
    out = adapter.handle(REQUEST)
    assert {c["verdict"] for c in out["evidence"]["checks"]} == {"UNCERTAIN"}
    assert out["evidence"]["decision"]["outcome"] == "pending_review" and comparer.requests == []


def test_a_damaged_return_is_reported_with_its_reasons(existing, tmp_path):
    config, capture, _ = existing
    store = _library(tmp_path, capture.tenant.organization_id, capture.order.ordered_sku)
    damaged = {**GOOD, "damage": _finding("severe", ref=()), "completeness": _finding("incomplete", ref=(), missing_components=["charger"])}
    out = _adapter(existing, store, ScriptedComparer(damaged))[0].handle(REQUEST)
    record = out["evidence"]
    assert out["verdict"] == "FAIL" and record["decision"]["outcome"] == "dispose" and record["decision"]["needs_human"] is True
    assert out["next_step_recommendation"]["action"] == "review"
    assert "charger" in next(c for c in record["checks"] if c["check_key"] == "reference_completeness")["detail"]


def test_model_failure_is_recorded_and_never_a_pass(existing, tmp_path):
    config, capture, _ = existing
    store = _library(tmp_path, capture.tenant.organization_id, capture.order.ordered_sku)
    out = _adapter(existing, store, ScriptedComparer(TimeoutError("slow")))[0].handle(REQUEST)
    assert not errors("agent-output", out) and verify(out["evidence"])
    assert out["status"] == "pending" and out["error"]["code"] == "provider_timeout" and out["verdict"] == "UNCERTAIN"
    assert out["evidence"]["decision"]["outcome"] == "pending_review" and out["evidence"]["decision"]["needs_human"]
