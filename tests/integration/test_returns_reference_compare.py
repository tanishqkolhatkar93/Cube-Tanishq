"""Reference-image comparison for Returns: strict parsing, rules, stores and the Gemini request. No network."""
import hashlib
import io
import json

import pytest

from agents.returns.core.returns_manager.gemini import GeminiConfig
from agents.returns.core.returns_manager.vision import ProviderUnavailable
from agents.returns.reference_compare import (
    GeminiReferenceComparer, ReturnedImage, ScriptedComparer, compare,
)
from agents.returns.reference_store import (
    DirReferenceStore, MAX_REFERENCE_IMAGES, ReferenceStoreError, SupabaseReferenceStore,
)
from tests.integration.fake_supabase import SERVICE_KEY, URL, FakeSupabase

ORG, SKU = "org_demo_alpha", "KB-001"


def jpeg(color, size=(96, 64)) -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "JPEG")
    return out.getvalue()


def returned(n=2, role="returned_product", content=None):
    return [ReturnedImage(f"UNIT-1/returns/IMG_{i}.jpg", f"EVD-{i}", role if i == 1 else "returned_packaging",
                          hashlib.sha256(content or jpeg((i * 40, 10, 10))).hexdigest(), content or jpeg((i * 40, 10, 10)))
            for i in range(1, n + 1)]


def finding(state, *, ret=("RET-1",), ref=("REF-1",), reason="visible in the photos", confidence=0.9, **extra):
    return {"state": state, "reason": reason, "returned_ids": list(ret), "reference_ids": list(ref), "confidence": confidence, **extra}


def answer(identity="same", completeness="complete", damage="none", condition="as_reference", **overrides):
    doc = {"identity": finding(identity), "completeness": finding(completeness, ref=[]),
           "damage": finding(damage, ref=[]), "condition": finding(condition)}
    if completeness == "incomplete":
        doc["completeness"]["missing_components"] = ["charger"]
    doc.update(overrides)
    return doc


@pytest.fixture()
def library(tmp_path):
    root = tmp_path / "refs"
    for angle, color in (("front", (200, 0, 0)), ("back", (0, 200, 0))):
        folder = root / ORG / SKU / angle
        folder.mkdir(parents=True)
        (folder / f"{angle}.jpg").write_bytes(jpeg(color))
    return DirReferenceStore(root)


def run(library, comparer, imgs=None, sku=SKU, org=ORG):
    metas = library.list(org, sku)
    return compare(org_id=org, sku=sku, expected_components=("keyboard", "charger"), returned=imgs if imgs is not None else returned(),
                   metas=metas, store=library, comparer=comparer)


def verdicts(result):
    return {c["check_key"]: c["verdict"] for c in result.checks}


# --- decisions -----------------------------------------------------------------------------------

def test_clean_return_is_restocked_and_cites_both_photo_sets(library):
    comparer = ScriptedComparer(answer())
    result = run(library, comparer)
    assert set(verdicts(result).values()) == {"PASS"} and len(result.checks) == 4
    assert (result.disposition, result.needs_human, result.error_code) == ("restock", False, None)
    refs = next(c for c in result.checks if c["check_key"] == "reference_identity")["evidence_refs"]
    assert any(r.startswith("UNIT-1/returns/") for r in refs) and any(r.startswith("reference:org/") for r in refs)
    assert result.model["calls"] == 1 and result.model["prompt_version"]
    assert len(comparer.requests) == 1, "exactly one batched model call per returned unit"


def test_wrong_item_goes_to_a_person_never_auto_disposed(library):
    result = run(library, ScriptedComparer(answer(identity="different")))
    assert verdicts(result)["reference_identity"] == "FAIL"
    assert (result.disposition, result.needs_human, result.reasons) == ("pending_review", True, ("wrong_item_returned",))


def test_cannot_tell_is_uncertain_not_pass(library):
    result = run(library, ScriptedComparer(answer(damage="cannot_tell")))
    assert verdicts(result)["reference_damage"] == "UNCERTAIN"
    assert result.disposition == "pending_review" and result.needs_human
    assert next(c for c in result.checks if c["check_key"] == "reference_damage")["uncertain_reason"] == "insufficient_evidence"


@pytest.mark.parametrize("kwargs, expected", [
    ({"damage": "minor"}, "refurbish"),
    ({"completeness": "incomplete"}, "refurbish"),
    ({"condition": "worse"}, "refurbish"),
    ({"damage": "severe"}, "liquidate"),
    ({"damage": "severe", "completeness": "incomplete"}, "dispose"),
])
def test_disposition_policy(library, kwargs, expected):
    result = run(library, ScriptedComparer(answer(**kwargs)))
    assert result.disposition == expected and result.needs_human


def test_pass_without_citations_is_not_accepted(library):
    uncited = answer(damage="none")
    uncited["damage"]["returned_ids"] = []
    result = run(library, ScriptedComparer(uncited))
    assert verdicts(result)["reference_damage"] == "UNCERTAIN"
    assert result.disposition == "pending_review"


def test_incomplete_must_name_what_is_missing(library):
    doc = answer(completeness="incomplete")
    doc["completeness"]["missing_components"] = []
    result = run(library, ScriptedComparer(doc))
    assert verdicts(result)["reference_completeness"] == "UNCERTAIN"


def test_identity_needs_a_reference_citation_too(library):
    doc = answer()
    doc["identity"]["reference_ids"] = []
    assert verdicts(run(library, ScriptedComparer(doc)))["reference_identity"] == "UNCERTAIN"


# --- the model cannot smuggle in decisions or unknown evidence --------------------------------------

@pytest.mark.parametrize("bad", [
    "not json at all",
    "[]",
    json.dumps({"identity": finding("same")}),                                                    # missing findings
    json.dumps({**answer(), "disposition": "restock"}),                                           # extra top-level key
    json.dumps({**answer(), "identity": {**finding("same"), "verdict": "PASS"}}),                 # model tries to set a verdict
    json.dumps({**answer(), "identity": finding("same", ret=("RET-9",))}),                        # cites an image never provided
    json.dumps({**answer(), "identity": finding("same", confidence=1.5)}),                        # out of range
    json.dumps({**answer(), "identity": finding("maybe")}),                                       # unknown state
    '{"identity": {"state": "same", "state": "different"}}',                                      # duplicate key
])
def test_malformed_or_overreaching_model_output_is_rejected_and_recorded(library, bad):
    result = run(library, ScriptedComparer(bad))
    assert set(verdicts(result).values()) == {"UNCERTAIN"}
    assert result.error_code == "invalid_response" and result.disposition == "pending_review"
    assert all(c["uncertain_reason"] == "model_error" for c in result.checks)


@pytest.mark.parametrize("failure, code", [(TimeoutError("slow"), "provider_timeout"), (ProviderUnavailable("down"), "provider_unavailable")])
def test_model_outage_never_becomes_a_verdict(library, failure, code):
    result = run(library, ScriptedComparer(failure))
    assert result.error_code == code and set(verdicts(result).values()) == {"UNCERTAIN"} and result.needs_human


# --- inputs that make a comparison impossible are UNCERTAIN, and cost no model call ------------------

def test_no_reference_photos_means_uncertain_without_calling_the_model(library):
    comparer = ScriptedComparer(answer())
    result = run(library, comparer, sku="UNKNOWN-SKU")
    assert set(verdicts(result).values()) == {"UNCERTAIN"} and result.reasons == ("no_reference_images",)
    assert result.model["calls"] == 0 and comparer.requests == []


def test_unreadable_returned_photos_are_poor_image_without_a_model_call(library):
    comparer = ScriptedComparer(answer())
    result = run(library, comparer, imgs=returned(content=b"not an image"))
    assert result.reasons == ("no_readable_returned_photos",) and comparer.requests == []
    assert all(c["uncertain_reason"] == "poor_image" for c in result.checks)


def test_reference_tampering_between_listing_and_judging_is_caught(library):
    metas = library.list(ORG, SKU)
    (library.root / metas[0].locator).write_bytes(jpeg((1, 2, 3)))          # swapped after registration
    comparer = ScriptedComparer(answer())
    result = compare(org_id=ORG, sku=SKU, expected_components=(), returned=returned(), metas=metas, store=library, comparer=comparer)
    assert result.error_code == "reference_image_changed" and comparer.requests == []


# --- reference stores ---------------------------------------------------------------------------

def test_dir_store_is_tenant_scoped_and_refuses_unsafe_names(library):
    assert {m.angle for m in library.list(ORG, SKU)} == {"front", "back"}
    assert library.list("org_demo_bravo", SKU) == ()                         # other tenant sees nothing
    for sku in ("../org_demo_alpha/KB-001", "KB 001", "", "..", "a/b"):
        assert library.list(ORG, sku) == ()
    assert library.list("../x", SKU) == ()


def test_dir_store_caps_the_number_of_references(tmp_path):
    for i in range(MAX_REFERENCE_IMAGES + 4):
        folder = tmp_path / ORG / SKU / f"angle{i:02d}"
        folder.mkdir(parents=True)
        (folder / "x.jpg").write_bytes(jpeg((i, i, i)))
    assert len(DirReferenceStore(tmp_path).list(ORG, SKU)) == MAX_REFERENCE_IMAGES


def supabase_library():
    fake = FakeSupabase()
    store = SupabaseReferenceStore(URL, SERVICE_KEY, client=fake.client())
    store.add_product(ORG, SKU, "Keyboard", ["keyboard", "charger"])
    meta = store.add_image(ORG, SKU, "front", "front.jpg", jpeg((200, 0, 0)), "tester")
    store.add_image(ORG, SKU, "back", "back.jpg", jpeg((0, 200, 0)), "tester")
    return fake, store, meta


def test_supabase_store_round_trip_and_integrity():
    fake, store, meta = supabase_library()
    metas = store.list(ORG, SKU)
    assert [m.angle for m in metas] == ["back", "front"]
    assert all(m.ref.startswith(f"reference:org/{ORG}/reference/{SKU}/") for m in metas)
    assert hashlib.sha256(store.load(metas[1])).hexdigest() == meta.sha256
    fake.objects[f"cube-inputs/{meta.locator}"] = jpeg((9, 9, 9))            # object replaced in storage
    with pytest.raises(ReferenceStoreError) as raised:
        store.load(metas[1])
    assert str(raised.value) == "reference_image_changed"


def test_supabase_store_scopes_by_org_and_ignores_rows_pointing_elsewhere():
    fake, store, _ = supabase_library()
    assert store.list("org_demo_bravo", SKU) == ()
    fake.rows("reference_images").append({"org_id": ORG, "sku": SKU, "angle": "evil", "sha256": "0" * 64,
                                          "storage_path": "org/org_demo_bravo/reference/KB-001/front/x.jpg"})
    fake.rows("reference_images").append({"org_id": ORG, "sku": SKU, "angle": "dots", "sha256": "0" * 64,
                                          "storage_path": f"org/{ORG}/reference/{SKU}/../../x.jpg"})
    assert {m.angle for m in store.list(ORG, SKU)} == {"front", "back"}


def test_supabase_store_outage_and_key_hygiene():
    fake, store, _ = supabase_library()
    fake.down = True
    with pytest.raises(ReferenceStoreError) as raised:
        store.list(ORG, SKU)
    assert SERVICE_KEY not in str(raised.value) and "supabase.co" not in str(raised.value)
    with pytest.raises(ValueError):
        store.add_image(ORG, SKU, "front", "../evil.jpg", b"x", "tester")


def test_a_comparison_runs_end_to_end_from_the_supabase_library():
    _, store, _ = supabase_library()
    result = compare(org_id=ORG, sku=SKU, expected_components=("keyboard",), returned=returned(), metas=store.list(ORG, SKU),
                     store=store, comparer=ScriptedComparer(lambda req: answer()))
    assert result.disposition == "restock"


# --- the real Gemini request, built and parsed offline ---------------------------------------------

def gemini_envelope(text):
    return json.dumps({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": text}]}}],
                       "modelVersion": "gemini-3.8-flash-001", "usageMetadata": {"totalTokenCount": 1234}}).encode()


def test_gemini_request_shape_and_response_parsing(library):
    seen = {}

    def transport(url, payload, timeout, api_key):
        seen.update(url=url, payload=payload, timeout=timeout, key=api_key)
        return gemini_envelope(json.dumps(answer()))

    comparer = GeminiReferenceComparer(GeminiConfig("k" * 39, "gemini-3.8-flash", 20, True), transport=transport)
    result = run(library, comparer)
    assert result.disposition == "restock" and result.model["version"] == "gemini-3.8-flash-001" and result.model["calls"] == 1

    parts = seen["payload"]["contents"][0]["parts"]
    context = json.loads(parts[0]["text"])
    assert [i["id"] for i in context["images_in_attachment_order"]] == ["REF-1", "REF-2", "RET-1", "RET-2"]
    assert len(parts) == 1 + 4 and all(p["inlineData"]["mimeType"] == "image/jpeg" for p in parts[1:])
    config = seen["payload"]["generationConfig"]
    assert config["temperature"] == 0 and config["responseMimeType"] == "application/json" and "responseJsonSchema" in config
    assert config["maxOutputTokens"] >= 4096, "thinking tokens share this budget; 2048 truncated real answers"
    assert "k" * 39 not in json.dumps(seen["payload"]), "the API key must never be placed in the request body"
    assert seen["url"].endswith("gemini-3.8-flash:generateContent")


def test_gemini_comparer_fails_closed_without_key_or_confirmation(library):
    for config in (GeminiConfig("", "gemini-3.8-flash", 20, True), GeminiConfig("k" * 39, "gemini-3.8-flash", 20, False)):
        result = run(library, GeminiReferenceComparer(config, transport=lambda *a: pytest.fail("must not call out")))
        assert result.error_code == "provider_unavailable" and set(verdicts(result).values()) == {"UNCERTAIN"}


@pytest.mark.parametrize("envelope", [
    {"error": {"message": "x"}},
    {"candidates": [{"finishReason": "SAFETY", "content": {"parts": [{"text": "{}"}]}}]},
    # measured live: thinking exhausted the token budget and the JSON answer was cut off mid-way
    {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": '{"identity": {"state": "same", "reas'}]}}]},
    {"promptFeedback": {"blockReason": "OTHER"}, "candidates": []},
])
def test_blocked_or_incomplete_gemini_responses_are_rejected(library, envelope):
    comparer = GeminiReferenceComparer(GeminiConfig("k" * 39, "gemini-3.8-flash", 20, True), transport=lambda *a: json.dumps(envelope).encode())
    assert run(library, comparer).error_code == "invalid_response"
