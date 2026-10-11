"""Compare a returned item's photos with the catalogue reference photos using a vision model.

Division of labour (same principle as the pinned Returns engine):
  * the MODEL reports observations only: for four questions, a state, a short reason and the IDs of the
    images it relied on. It never produces verdicts, dispositions or business decisions;
  * PURE RULES in this module turn those observations into PASS / FAIL / UNCERTAIN and a disposition.

Nothing is guessed. UNCERTAIN (never PASS) results when: no reference photos exist, no readable returned
photos exist, the model is unavailable / times out / returns malformed JSON, the model says it cannot tell,
or a PASS/FAIL finding does not cite the images it is based on.

No image hashing is used to decide similarity. SHA-256 only records WHICH bytes were judged.
Design: docs/product/returns-image-verification.md. Disposition policy: docs/decisions.md D-RETURNS-DISPOSITION.
"""
from __future__ import annotations

import base64
import io
import json
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from shared.utils.records import check

from .core.returns_manager.domain import ValidationError
from .core.returns_manager.gemini import MAX_RESPONSE_BYTES, GeminiConfig, _unique, post_json
from .core.returns_manager.vision import ProviderUnavailable, image_availability
from .reference_store import ReferenceMeta, ReferenceStore, ReferenceStoreError

PROMPT_VERSION = "returns-reference-compare-v1"
MAX_MODEL_SIDE_PX = 1280
MAX_REASON_CHARS = 400
# The budget also pays for the model's internal "thinking". 2048 was measured live to be exhausted by thinking
# alone (finishReason=MAX_TOKENS, JSON cut off), so the answer was rejected. Keep this comfortably above that.
MAX_OUTPUT_TOKENS = 8192

# check name -> (record check_key, allowed states, state -> verdict for decided states)
RULES = {
    "identity": ("reference_identity", ("same", "different", "cannot_tell"), {"same": "PASS", "different": "FAIL"}),
    "completeness": ("reference_completeness", ("complete", "incomplete", "cannot_tell"), {"complete": "PASS", "incomplete": "FAIL"}),
    "damage": ("reference_damage", ("none", "minor", "severe", "cannot_tell"), {"none": "PASS", "minor": "FAIL", "severe": "FAIL"}),
    "condition": ("reference_condition", ("as_reference", "worse", "cannot_tell"), {"as_reference": "PASS", "worse": "FAIL"}),
}


# --------------------------------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ReturnedImage:
    ref: str            # input ref recorded in evidence, e.g. "UNIT-0014/returns/IMG_1.jpg"
    evidence_id: str
    role: str           # returned_product | returned_packaging
    sha256: str
    content: bytes = field(repr=False)


@dataclass(frozen=True)
class ComparisonRequest:
    sku: str
    expected_components: tuple[str, ...]
    references: tuple[tuple[str, ReferenceMeta, bytes], ...]   # (prompt id "REF-1", meta, bytes)
    returned: tuple[tuple[str, ReturnedImage], ...]            # (prompt id "RET-1", image)


@dataclass(frozen=True)
class Reply:
    text: str
    name: str
    model_version: str | None = None
    tokens: int | None = None
    calls: int = 1


class Comparer(Protocol):
    name: str
    mode: str
    prompt_version: str

    def compare(self, request: ComparisonRequest) -> Reply: ...


@dataclass(frozen=True)
class Finding:
    state: str
    reason: str
    returned_ids: tuple[str, ...]
    reference_ids: tuple[str, ...]
    confidence: float | None
    missing_components: tuple[str, ...] = ()


@dataclass(frozen=True)
class Comparison:
    checks: tuple[dict, ...]
    disposition: str
    needs_human: bool
    reasons: tuple[str, ...]
    model: dict
    error_code: str | None
    findings: dict
    references: tuple[dict, ...]


# --------------------------------------------------------------------------------------------------
# Strict parsing of the model's observations
# --------------------------------------------------------------------------------------------------

def response_schema() -> dict:
    """JSON schema sent to the model (a subset Gemini accepts). Local parsing below is authoritative."""
    def finding(states, extra=None):
        properties = {"state": {"type": "string", "enum": list(states)}, "reason": {"type": "string"},
                      "returned_ids": {"type": "array", "items": {"type": "string"}},
                      "reference_ids": {"type": "array", "items": {"type": "string"}},
                      "confidence": {"type": "number"}, **(extra or {})}
        return {"type": "object", "properties": properties, "required": ["state", "reason", "returned_ids", "reference_ids"]}
    return {"type": "object", "required": list(RULES), "properties": {
        "identity": finding(RULES["identity"][1]),
        "completeness": finding(RULES["completeness"][1], {"missing_components": {"type": "array", "items": {"type": "string"}}}),
        "damage": finding(RULES["damage"][1]),
        "condition": finding(RULES["condition"][1])}}


def _ids(value, allowed: set[str], label: str) -> tuple[str, ...]:
    if type(value) is not list or len(value) > 20 or any(type(i) is not str for i in value):
        raise ValidationError(f"{label} must be a short list of image IDs")
    if not set(value) <= allowed:
        raise ValidationError(f"{label} cites an image that was not provided")
    return tuple(dict.fromkeys(value))


def parse_findings(text: str, returned_ids: set[str], reference_ids: set[str]) -> dict[str, Finding]:
    try:
        raw = json.loads(text, object_pairs_hook=_unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
    except (ValueError, RecursionError):
        raise ValidationError("model did not return valid JSON") from None
    if type(raw) is not dict or set(raw) != set(RULES):
        raise ValidationError("model response must contain exactly the four findings")
    parsed = {}
    for name, (_, states, _) in RULES.items():
        item = raw[name]
        allowed_keys = {"state", "reason", "returned_ids", "reference_ids", "confidence"} | ({"missing_components"} if name == "completeness" else set())
        required = {"state", "reason", "returned_ids", "reference_ids"}
        if type(item) is not dict or not required <= set(item) <= allowed_keys:
            raise ValidationError(f"{name}: unexpected fields")
        if item["state"] not in states:
            raise ValidationError(f"{name}: unknown state")
        if type(item["reason"]) is not str or not item["reason"].strip():
            raise ValidationError(f"{name}: a reason is required")
        confidence = item.get("confidence")
        if confidence is not None and (type(confidence) not in (int, float) or isinstance(confidence, bool)
                                       or not math.isfinite(confidence) or not 0 <= confidence <= 1):
            raise ValidationError(f"{name}: confidence must be between 0 and 1")
        missing = item.get("missing_components", [])
        if type(missing) is not list or len(missing) > 20 or any(type(m) is not str or not m.strip() or len(m) > 80 for m in missing):
            raise ValidationError(f"{name}: invalid missing_components")
        parsed[name] = Finding(item["state"], item["reason"].strip()[:MAX_REASON_CHARS],
                               _ids(item["returned_ids"], returned_ids, f"{name}.returned_ids"),
                               _ids(item["reference_ids"], reference_ids, f"{name}.reference_ids"),
                               None if confidence is None else float(confidence),
                               tuple(m.strip() for m in missing))
    return parsed


# --------------------------------------------------------------------------------------------------
# Rules: observations -> verdicts -> disposition (pure, no I/O)
# --------------------------------------------------------------------------------------------------

def judge(name: str, finding: Finding) -> tuple[str, str | None]:
    """-> (verdict, uncertain_reason). A decided finding must cite what it relied on."""
    verdict = RULES[name][2].get(finding.state)
    if verdict is None:
        return "UNCERTAIN", "insufficient_evidence"
    needs_reference = name == "identity"
    if not finding.returned_ids or (needs_reference and not finding.reference_ids):
        return "UNCERTAIN", "insufficient_evidence"          # uncited finding: not accepted
    if name == "completeness" and verdict == "FAIL" and not finding.missing_components:
        return "UNCERTAIN", "insufficient_evidence"          # "incomplete" must say what is missing
    return verdict, None


def recommend(verdicts: dict[str, str], findings: dict[str, Finding]) -> tuple[str, bool, tuple[str, ...]]:
    """Disposition recommendation. Policy documented in docs/decisions.md (D-RETURNS-DISPOSITION)."""
    undecided = sorted(k for k, v in verdicts.items() if v == "UNCERTAIN")
    if undecided:
        return "pending_review", True, ("uncertain_checks:" + ",".join(undecided),)
    if verdicts["identity"] == "FAIL":
        return "pending_review", True, ("wrong_item_returned",)
    if all(v == "PASS" for v in verdicts.values()):
        return "restock", False, ("all_reference_checks_passed",)
    reasons = []
    if findings["damage"].state == "severe":
        reasons.append("severe_damage")
        if verdicts["completeness"] == "FAIL":
            return "dispose", True, (*reasons, "components_missing")
        return "liquidate", True, tuple(reasons)
    if findings["damage"].state == "minor":
        reasons.append("minor_damage")
    if verdicts["completeness"] == "FAIL":
        reasons.append("components_missing")
    if verdicts["condition"] == "FAIL":
        reasons.append("condition_worse_than_reference")
    return "refurbish", True, tuple(reasons)


# --------------------------------------------------------------------------------------------------
# Orchestration of one comparison
# --------------------------------------------------------------------------------------------------

def _undecided(reason_code: str, detail: str, uncertain_reason: str, model: dict, refs: tuple[dict, ...], error: str | None = None) -> Comparison:
    checks = tuple(check(key, "UNCERTAIN", None, detail=detail, uncertain_reason=uncertain_reason) for key, _, _ in RULES.values())
    return Comparison(checks, "pending_review", True, (reason_code,), model, error, {}, refs)


def compare(*, org_id: str, sku: str, expected_components, returned: list[ReturnedImage], metas: tuple[ReferenceMeta, ...],
            store: ReferenceStore | None, comparer: Comparer | None, list_error: str | None = None) -> Comparison:
    refs_summary = tuple({"ref": m.ref, "angle": m.angle, "sha256": m.sha256} for m in metas)
    base_model = {"name": getattr(comparer, "name", "rules"), "version": "unknown", "calls": 0,
                  "prompt_version": getattr(comparer, "prompt_version", PROMPT_VERSION)}
    if list_error:
        return _undecided(list_error, "The reference library could not be read, so nothing was compared.",
                          "model_error", {**base_model, "version": "rules"}, refs_summary, list_error)
    if not metas:
        return _undecided("no_reference_images", "No reference photos are registered for this SKU, so nothing can be compared.",
                          "insufficient_evidence", {**base_model, "version": "rules"}, refs_summary)
    readable = [i for i in returned if i.role in {"returned_product", "returned_packaging"} and image_availability(i.content) == "available"]
    if not any(i.role == "returned_product" for i in readable):
        return _undecided("no_readable_returned_photos", "No readable photo of the returned product is available.",
                          "poor_image", {**base_model, "version": "rules"}, refs_summary)
    if store is None or comparer is None:
        return _undecided("comparison_not_configured", "Reference comparison is not configured.",
                          "rule_unavailable", {**base_model, "version": "rules"}, refs_summary)
    try:
        loaded = tuple((f"REF-{n}", m, store.load(m)) for n, m in enumerate(metas, 1))
    except ReferenceStoreError as exc:
        return _undecided(str(exc), "Reference photos could not be read or no longer match their registered hash.",
                          "model_error", {**base_model, "version": "rules"}, refs_summary, str(exc))
    request = ComparisonRequest(sku, tuple(expected_components), loaded, tuple((f"RET-{n}", i) for n, i in enumerate(readable, 1)))
    started = time.perf_counter()
    try:
        reply = comparer.compare(request)
        findings = parse_findings(reply.text, {rid for rid, _ in request.returned}, {rid for rid, _, _ in request.references})
    except TimeoutError:
        return _undecided("provider_timeout", "The vision model timed out; no comparison was made.", "model_error",
                          {**base_model, "calls": 1}, refs_summary, "provider_timeout")
    except ProviderUnavailable:
        return _undecided("provider_unavailable", "The vision model was unavailable; no comparison was made.", "model_error",
                          {**base_model, "calls": 1}, refs_summary, "provider_unavailable")
    except (ValidationError, ValueError):
        return _undecided("invalid_response", "The vision model returned an unusable response; it was rejected.", "model_error",
                          {**base_model, "calls": 1}, refs_summary, "invalid_response")

    ref_label = {rid: m.ref for rid, m, _ in request.references}
    ret_label = {rid: i.ref for rid, i in request.returned}
    checks, verdicts = [], {}
    for name, (key, _, _) in RULES.items():
        finding = findings[name]
        verdict, uncertain = judge(name, finding)
        verdicts[name] = verdict
        cited = [ret_label[i] for i in finding.returned_ids] + [ref_label[i] for i in finding.reference_ids]
        detail = finding.reason if verdict != "UNCERTAIN" or finding.state == "cannot_tell" else f"{finding.reason} (finding not accepted: {finding.state} without required citations)"
        if name == "completeness" and finding.missing_components:
            detail += " Missing: " + ", ".join(finding.missing_components) + "."
        checks.append(check(key, verdict, finding.confidence, observed=finding.state, detail=detail,
                            evidence_refs=cited or None, uncertain_reason=uncertain))
    disposition, needs_human, reasons = recommend(verdicts, findings)
    model = {"name": reply.name, "version": reply.model_version or "unknown", "provider": reply.name,
             "prompt_version": getattr(comparer, "prompt_version", PROMPT_VERSION), "calls": reply.calls}
    payload = {name: {"state": f.state, "reason": f.reason, "confidence": f.confidence,
                      "returned": [ret_label[i] for i in f.returned_ids], "reference": [ref_label[i] for i in f.reference_ids],
                      **({"missing_components": list(f.missing_components)} if name == "completeness" else {})}
               for name, f in findings.items()}
    payload["_latency_ms"] = int((time.perf_counter() - started) * 1000)
    return Comparison(tuple(checks), disposition, needs_human, reasons, model, None, payload, refs_summary)


# --------------------------------------------------------------------------------------------------
# Comparers
# --------------------------------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You compare photos of a RETURNED item against catalogue REFERENCE photos of the product it is supposed to be. "
    "Image ids starting REF- are catalogue references; ids starting RET- are the customer's returned photos. "
    "Report only what is visible. Never guess: use state 'cannot_tell' whenever the photos do not show enough "
    "(blurry, dark, hidden, wrong angle, nothing to compare). Packaging or printed text alone does not prove which "
    "product is inside. Do not decide refunds, restocking or any business outcome. For every finding, list the ids "
    "of the images you relied on in returned_ids (RET-...) and reference_ids (REF-...). "
    "identity: same = clearly the same product model as the references; different = clearly a different product. "
    "completeness: compare with expected_components and the references; incomplete requires missing_components. "
    "damage: none / minor (cosmetic) / severe (affects use or appearance a lot) in the returned photos. "
    "condition: as_reference = looks like the reference condition; worse = visibly worse. "
    "Keep each reason to one short sentence. Respond with JSON only."
)


def _downscale(content: bytes) -> bytes:
    """Shrink for the model (cost, latency, request-size). The recorded SHA-256 stays that of the original."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(content), formats=("PNG", "JPEG")) as image:
            image = image.convert("RGB")
            image.thumbnail((MAX_MODEL_SIDE_PX, MAX_MODEL_SIDE_PX))
            out = io.BytesIO()
            image.save(out, "JPEG", quality=85)
            return out.getvalue()
    except Exception:
        raise ValidationError("image could not be prepared for the model") from None


class GeminiReferenceComparer:
    """One batched Gemini call per returned unit. Reuses the Returns engine's bounded, redirect-safe transport."""
    name = "gemini"
    mode = "real"
    prompt_version = PROMPT_VERSION

    def __init__(self, config: GeminiConfig | None = None, *, transport: Callable = post_json):
        self.config = config if config is not None else GeminiConfig.from_env()
        self.transport = transport

    def compare(self, request: ComparisonRequest) -> Reply:
        if not self.config.api_key or not self.config.free_tier_confirmed:
            raise ProviderUnavailable("gemini_key_missing" if not self.config.api_key else "gemini_free_tier_confirmation_required")
        ordered = [(rid, "reference", m.angle, content) for rid, m, content in request.references] + \
                  [(rid, "returned", i.role, i.content) for rid, i in request.returned]
        parts = []
        for _, _, _, content in ordered:
            parts.append({"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(_downscale(content)).decode("ascii")}})
        context = {"sku": request.sku, "expected_components": list(request.expected_components),
                   "images_in_attachment_order": [{"id": rid, "kind": kind, "angle_or_role": label} for rid, kind, label, _ in ordered]}
        payload = {"systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                   "contents": [{"role": "user", "parts": [{"text": json.dumps(context, separators=(",", ":"))}, *parts]}],
                   "generationConfig": {"temperature": 0, "maxOutputTokens": MAX_OUTPUT_TOKENS, "responseMimeType": "application/json",
                                        "responseJsonSchema": response_schema()}}
        raw = self.transport(self.config.endpoint, payload, self.config.timeout_seconds, self.config.api_key)
        if type(raw) is not bytes or len(raw) > MAX_RESPONSE_BYTES:
            raise ValidationError("bounded response required")
        try:
            envelope = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
        except (ValueError, RecursionError):
            raise ValidationError("invalid provider JSON") from None
        candidates = envelope.get("candidates") if type(envelope) is dict else None
        if (type(envelope) is not dict or "error" in envelope or envelope.get("promptFeedback", {}).get("blockReason")
                or type(candidates) is not list or len(candidates) != 1 or candidates[0].get("finishReason") != "STOP"):
            raise ValidationError("provider blocked or incomplete response")
        texts = []
        for part in candidates[0].get("content", {}).get("parts") or []:
            if type(part) is not dict or type(part.get("text")) is not str:
                raise ValidationError("unexpected provider output part")
            if not part.get("thought", False):
                texts.append(part["text"])
        tokens = envelope.get("usageMetadata", {}).get("totalTokenCount")
        return Reply("".join(texts), self.name, envelope.get("modelVersion"), tokens if type(tokens) is int else None)


class ScriptedComparer:
    """Test double: answers with a fixed or computed JSON document. Never impersonates a real provider."""
    name = "fixture-json"
    mode = "fixture"
    prompt_version = PROMPT_VERSION

    def __init__(self, answer):
        self.answer, self.requests = answer, []

    def compare(self, request: ComparisonRequest) -> Reply:
        self.requests.append(request)
        value = self.answer(request) if callable(self.answer) else self.answer
        if isinstance(value, Exception):
            raise value
        return Reply(value if isinstance(value, str) else json.dumps(value), self.name, "fixture-1")
