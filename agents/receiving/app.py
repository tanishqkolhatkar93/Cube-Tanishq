"""Receiving adapter: registered captures, typed observations, deterministic policy and durable replay."""
from __future__ import annotations

import hashlib
import json
import time
import os
from pathlib import Path

from shared.utils import sample_data
from shared.utils.records import (build_output, build_record, check,
                                  error_obj, rollup, utcnow)
from .safety import Rejected, Ledger, digest, validate_request, safe_ref as _safe_rel, resolve_captures, record_id as _record_id
from .core.failures import classify

from .core.config import CFG, DATA_INPUT
from .core.engine import run_engine
from .core.extraction.service import PROMPT_VERSION
from .core.models import POLineItem, CheckContext, ImageObservation, ObsProvenance

STAGE = "receiving"
AGENT_ID = "receiving-manager@1.0.0"
VERSION = "1.0.0"

REGISTRY = Path(os.environ.get("RECEIVING_UNIT_REGISTRY", Path(__file__).resolve().parent / "fixtures" / "units.json"))

REASON_MAP = {"INSUFFICIENT_EVIDENCE": "insufficient_evidence", "OCCLUSION": "occluded",
              "LOW_IMAGE_QUALITY": "poor_image", "CONFLICTING_EVIDENCE": "conflicting_evidence",
              "EXTRACTION_FAILED": "model_error"}
DAMAGE_VOCAB = {"tear_or_open": "tears", "crushing": "crushing", "water": "water",
                "dent": "dent", "other": "other", "none": "none"}


# ---------------------------------------------------------------- security
def _resolve_inputs(request, subject_id):
    registry = Path(os.environ.get("RECEIVING_CAPTURE_REGISTRY", Path(__file__).parent / "fixtures" / "captures.json"))
    return resolve_captures(request, DATA_INPUT, registry)


# ---------------------------------------------------------------- lookup / specs
def load_registry() -> dict:
    if REGISTRY.exists():
        return json.loads(REGISTRY.read_text(encoding="utf-8")).get("units", {})
    return {}


def _lookup(subject_id: str, org_id: str):
    """Tenancy: fixture registry (org-checked) then sample rows (org-scoped)."""
    u = load_registry().get(subject_id)
    if u is not None:
        if u.get("org_id") != org_id:
            raise LookupError(f"{subject_id} is not in {org_id}")
        return u, "fixture"
    return sample_data.row("receiving", subject_id, org_id), "sample"   # LookupError -> 404


def _na(v):
    return None if (v is None or str(v).strip().lower() in ("", "n/a", "na")) else v


def _spec_of(unit, source) -> dict:
    if source == "fixture":
        r = unit
    else:  # sample CSV row -> shared shape
        r = {"sku": unit["sku"], "asin": unit.get("asin"), "product_title": unit.get("product_title"),
             "spec_colour": _na(unit.get("spec_colour")), "spec_variant": _na(unit.get("spec_variant")),
             "spec_components": [c.strip() for c in unit["spec_components"].split(";") if c.strip()] or None
                if unit.get("spec_components") else None,
             "cartons_ordered": int(unit["cartons_ordered"]) if unit.get("cartons_ordered") else None,
             "units_per_carton_ordered": int(unit["units_per_carton_ordered"]) if unit.get("units_per_carton_ordered") else None,
             "qty_ordered": int(unit["qty_ordered"]), "po_number": unit.get("po_number"),
             "po_line": unit.get("po_line"), "supplier": unit.get("supplier"),
             "operator_id": unit.get("operator_id"), "captured_at": unit.get("captured_at")}
    return {"sku": r["sku"], "asin": r.get("asin"), "product_title": r.get("product_title"),
            "spec_colour": _na(r.get("spec_colour")), "spec_variant": _na(r.get("spec_variant")),
            "spec_components": r.get("spec_components") or None,
            "cartons_ordered": r.get("cartons_ordered"),
            "units_per_carton_ordered": r.get("units_per_carton_ordered"),
            "qty_ordered": int(r["qty_ordered"]), "po_number": r.get("po_number"),
            "po_line": r.get("po_line"), "supplier": r.get("supplier"),
            "operator_id": r.get("operator_id"), "captured_at": r.get("captured_at")}


# ---------------------------------------------------------------- ids / pending
def _pending(request, *, code, message, retryable=True, spec=None, inputs=None, stats=None, issues=None):
    spec = spec or {}
    cap, source = _captured_at(request, spec)
    record = build_record(request, agent_id=AGENT_ID, record_id=_record_id(request), captured_at=cap,
        checks=[], outcome="pending_review", reason=message,
        model=_model_info(stats or {"calls":0,"model_ids":[]}),
        status="pending" if retryable else "error", verdict="UNCERTAIN", needs_human=True,
        inputs=inputs or [], payload={"captured_at_source":source,"diagnostics":issues or [],
                                     "provider_attempts":(stats or {}).get("attempts",[]),
                                     "model_notes": {
                                         "vlm_calls": (stats or {}).get("calls", 0),
                                         "cached_extractions": (stats or {}).get("cached", 0),
                                         "rejected_by_gate": (stats or {}).get("rejected", 0),
                                         "per_image_models": (stats or {}).get("model_ids", [])}},
        error=error_obj(code,message,retryable=retryable,stage=STAGE,agent_id=AGENT_ID))
    return build_output(record, next_step="retry" if retryable else "review")

def _captured_at(request, spec):
    if spec.get("captured_at"):
        return spec["captured_at"], "trusted_spec"
    # Existing schema requires a timestamp even when no capture exists.
    return utcnow(), "unavailable_placeholder_not_a_capture_time"


# ---------------------------------------------------------------- check mapping
def _refs_for(result, ref_of):
    out = []
    for e in result.evidence:
        r = ref_of.get(e.image_id) if e.image_id else None
        if r and r not in out:
            out.append(r)
    return out or list(ref_of.values())


def _identity_check(by, ref_of, spec):
    parts = [by[k] for k in ("sku_identity", "colour", "variant")
             if k in by and by[k].verdict != "NOT_APPLICABLE"]
    exp = spec["sku"] + (f" ({spec['product_title']})" if spec.get("product_title") else "")
    fails = [p for p in parts if p.verdict == "FAIL"]
    uns = [p for p in parts if p.verdict == "UNCERTAIN"]
    if fails:
        p = fails[0]
        return check("identity_match", "FAIL", p.confidence, expected=exp, observed="no",
                     detail=p.detail, evidence_refs=_refs_for(p, ref_of))
    if uns:
        p = uns[0]
        return check("identity_match", "UNCERTAIN", p.confidence, expected=exp, observed="uncertain",
                     detail=p.detail, evidence_refs=_refs_for(p, ref_of),
                     uncertain_reason=REASON_MAP.get(p.uncertainty_reason, "insufficient_evidence"))
    p = parts[0]
    return check("identity_match", "PASS", p.confidence, expected=exp, observed="yes",
                 detail=p.detail, evidence_refs=_refs_for(p, ref_of))


def _direct_check(key, r, ref_of, expected):
    sv = r.summary_value
    if key in ("carton_damage", "unit_damage"):
        observed = DAMAGE_VOCAB.get(sv, sv) if sv else "uncertain"
    else:
        observed = sv if sv is not None else ("uncertain" if r.verdict == "UNCERTAIN" else None)
    kw = dict(expected=expected, observed=observed, detail=r.detail,
              evidence_refs=_refs_for(r, ref_of))
    if r.verdict == "UNCERTAIN":
        kw["uncertain_reason"] = REASON_MAP.get(r.uncertainty_reason, "insufficient_evidence")
    return check(key, r.verdict, r.confidence, **kw)


def _quality_flags_check(by, ref_of, flags):
    contributors = [by[k] for k in ("colour", "variant", "missing_components", "other_quality")
                    if k in by and by[k].verdict != "NOT_APPLICABLE"]
    refs = _refs_for(contributors[0], ref_of) if contributors else list(ref_of.values())
    fails = [c for c in contributors if c.verdict == "FAIL"]
    uns = [c for c in contributors if c.verdict == "UNCERTAIN"]
    if fails:
        return check("quality_flags", "FAIL", fails[0].confidence, expected=[], observed=flags,
                     detail="; ".join(c.detail for c in fails), evidence_refs=refs)
    if uns:
        return check("quality_flags", "UNCERTAIN", uns[0].confidence, expected=[], observed=[],
                     detail=f"flags not fully verifiable: {uns[0].detail}", evidence_refs=refs,
                     uncertain_reason=REASON_MAP.get(uns[0].uncertainty_reason, "insufficient_evidence"))
    return check("quality_flags", "PASS", 0.7, expected=[], observed=[],
                 detail="no quality flags raised across applicable checks", evidence_refs=refs)


def _contract_checks(by, ref_of, spec, summary):
    out = [_identity_check(by, ref_of, spec)]
    for key, exp in (("carton_count", spec.get("cartons_ordered")),
                     ("quantity", spec["qty_ordered"]),
                     ("units_per_carton", spec.get("units_per_carton_ordered")),
                     ("carton_damage", "none"), ("unit_damage", "none")):
        r = by.get(key)
        if r and r.verdict != "NOT_APPLICABLE":          # N/A -> omitted, per contract
            out.append(_direct_check(key, r, ref_of, exp))
    out.append(_quality_flags_check(by, ref_of, summary["quality_flags"]))
    return out


def _gate_rejected_checks(refs):
    detail = "every capture was rejected by the deterministic image-quality gate; nothing could be verified"
    return [check(k, "UNCERTAIN", None, expected=e, observed=o, detail=detail,
                  evidence_refs=refs, uncertain_reason="poor_image")
            for k, e, o in (("identity_match", None, "uncertain"), ("carton_count", None, "uncertain"),
                            ("quantity", None, "uncertain"), ("carton_damage", "none", "uncertain"),
                            ("unit_damage", "none", "uncertain"))] + [
        check("quality_flags", "UNCERTAIN", None, expected=[], observed=[],
              detail="image quality prevents verifying the absence of quality flags",
              evidence_refs=refs, uncertain_reason="poor_image")]


def _model_info(stats):
    ids = sorted(set(stats.get("model_ids", [])))
    return {"name": "gemini" if ids or stats["calls"] else "none",
            "version": ",".join(ids) if ids else "unavailable",
            "provider": "google" if ids or stats["calls"] else None,
            "prompt_version": PROMPT_VERSION, "calls":stats["calls"], "cost_usd":None}


# ---------------------------------------------------------------- entry point
def handle(request):
    try:
        request = json.loads(json.dumps(request, allow_nan=False))
    except (ValueError, TypeError):
        raise Rejected("invalid_receiving_request") from None
    validate_request(request)
    unit, source = _lookup(request["subject"]["subject_id"], request["subject"]["org_id"])
    try:
        spec = _spec_of(unit, source)
        POLineItem(**{k:v for k,v in spec.items() if k in POLineItem.model_fields})
    except (ValueError, TypeError, KeyError):
        raise Rejected("invalid_trusted_specification", 503) from None
    found, issues, capture_snapshot = _resolve_inputs(request, request["subject"]["subject_id"])
    fingerprint = digest({"request":request, "spec":spec, "captures":capture_snapshot,
        "policy":"receiving-safety-v1","model":CFG.gemini_model,
        "fallbacks":CFG.gemini_fallback_models,"prompt":PROMPT_VERSION})
    state = Path(os.environ.get("RECEIVING_STATE_DIR", Path(__file__).resolve().parents[2] / "out" / "receiving"))
    def produce():
        try:
            return _execute(request, unit, source, spec, found, issues)
        except Exception as exc:
            return _pending(request,code=classify(exc),message="Receiving could not complete inspection",spec=spec)
    return Ledger(state).execute(request, fingerprint, produce)

def _execute(request, unit, source, spec, found, issues):
    if issues or not found:
        rejected = any(i["code"] == "capture_rejected" for i in issues)
        return _pending(request,code="capture_rejected" if rejected else "upstream_missing",
                        message="Required capture set is unavailable or unauthorized", retryable=not rejected,
                        spec=spec,issues=issues)
    inputs_list, images, ref_of = [], [], {}
    for i, (inp, raw) in enumerate(found):
        sha = hashlib.sha256(raw).hexdigest()
        inputs_list.append({"ref": inp["ref"], "sha256": sha, "kind": "image"})
        images.append((inp["ref"], raw))
        ref_of[f"img_{i+1}"] = inp["ref"]

    t0 = time.time()
    if os.environ.get("RECEIVING_DEMO_MODE") == "1":
        stats = {"calls": 0, "cached": 0, "rejected": 0, "model_ids": ["local-demo-fixture"],
                 "attempts": [{"model": "local-demo-fixture", "outcome": "success"}]}
        provs = []
        for i, (ref, raw) in enumerate(images, start=1):
            provs.append(ObsProvenance(
                image_id=f"img_{i}",
                sha256=hashlib.sha256(raw).hexdigest(),
                observation=ImageObservation(
                    visible_sku_text=spec["sku"],
                    visible_product_text=spec.get("product_title"),
                    printed_quantity=spec.get("qty_ordered"),
                    visible_unit_count=spec.get("qty_ordered"),
                    count_confidence=0.92,
                    full_contents_visible=True,
                    cartons_visible=spec.get("cartons_ordered") or 1,
                    dominant_product_color=spec.get("spec_colour"),
                    label_colour_text=spec.get("spec_colour"),
                    label_variant_text=spec.get("spec_variant"),
                    visible_components=spec.get("spec_components"),
                    contents_open_for_inspection=True,
                    notes="Local demo observation from registered synthetic capture; no customer data.",
                ),
                quality={"verdict": "ACCEPTED", "reason": "local_demo_registered_capture"},
            ))
        errors = []
    else:
        provs, stats, errors = run_engine(images)
    usable = [p for p in provs if (p.quality or {}).get("verdict") != "REJECTED"]

    if errors:
        return _pending(request,code=errors[0]["code"],message="Required image extraction did not complete",
                        spec=spec,inputs=inputs_list,stats=stats,issues=errors)
    if len(usable) != len(provs) and usable:
        return _pending(request,code="image_quality_rejected",message="Required capture rejected by quality gate",
                        spec=spec,inputs=inputs_list,stats=stats,
                        issues=[{"code":"image_quality_rejected"}])


    if not usable:
        contract, summary = _gate_rejected_checks(list(ref_of.values())), None
        payload = {"granular_checks": [],
                   "photo_quality": {p.image_id: p.quality for p in provs if p.quality},
                   "model_notes": {"vlm_calls": 0, "rejected_by_gate": len(provs)}}
    else:
        from .core.checks import CHECKS              # registry import kept off the fail-open path
        from .core.summary import build_receiving_summary
        po = POLineItem(sku=spec["sku"], asin=spec.get("asin"), product_title=spec.get("product_title"),
                         spec_colour=spec.get("spec_colour"), spec_variant=spec.get("spec_variant"),
                         spec_components=spec.get("spec_components"),
                         cartons_ordered=spec.get("cartons_ordered"),
                         units_per_carton_ordered=spec.get("units_per_carton_ordered"),
                         qty_ordered=spec["qty_ordered"])
        ctx = CheckContext(po=po, observations=usable,
                           model_version=f"gemini:{_model_info(stats)['version']}")
        ours = [fn(ctx) for fn in CHECKS.values()]
        by = {r.check_key: r for r in ours}
        summary = build_receiving_summary(ours, po)
        contract = _contract_checks(by, ref_of, spec, summary)
        payload = {"supplier": spec.get("supplier"), "qty_ordered": spec["qty_ordered"],
                   "qty_received": summary["qty_received"],
                   "shortfall_units": (spec["qty_ordered"] - int(summary["qty_received"]))
                                      if summary["qty_received"].isdigit() else None,
                   "quality_flags": summary["quality_flags"], "receiving_summary": summary,
                   "granular_checks": [r.model_dump() for r in ours],
                   "photo_quality": {p.image_id: p.quality for p in provs if p.quality},
                   "model_notes": {"vlm_calls": stats["calls"], "cached_extractions": stats["cached"],
                                   "rejected_by_gate": stats["rejected"], "per_image_models": stats["model_ids"]},
                   "extraction_errors": errors or None,
                    "provider_attempts": stats.get("attempts", []),
                    "demo_mode": os.environ.get("RECEIVING_DEMO_MODE") == "1"}

    # Defensive invariant: exported verdict must include every applicable core check.
    verdict = rollup(contract)
    if usable:
        granular_verdict = rollup([{"verdict":r.verdict} for r in ours if r.verdict != "NOT_APPLICABLE"])
        if granular_verdict != verdict:
            contract.append(check("policy_aggregate",granular_verdict,None,
                detail="Aggregate of every applicable granular policy check",
                evidence_refs=list(ref_of.values())))
        verdict = rollup(contract)
    outcome = {"PASS": "accept", "FAIL": "accept_with_exceptions", "UNCERTAIN": "pending_review"}[verdict]
    failed = [c["check_key"] for c in contract if c["verdict"] == "FAIL"]
    unsure = [c["check_key"] for c in contract if c["verdict"] == "UNCERTAIN"]
    reason = ("all checks passed" if not (failed or unsure) else
              "; ".join([f"{k}=FAIL" for k in failed] + [f"{k}=UNCERTAIN" for k in unsure]))

    cap, cap_src = _captured_at(request, spec)
    payload["captured_at_source"] = cap_src
    payload["sample_record_id"] = unit.get("record_id") if source == "sample" else None

    confs = [c.get("confidence") for c in contract if c.get("confidence") is not None]
    record = build_record(
        request, agent_id=AGENT_ID, record_id=_record_id(request), captured_at=cap,
        checks=contract, outcome=outcome, reason=reason, model=_model_info(stats),
        unit_scope="po_line",
        refs={"po_number": spec.get("po_number"), "po_line": spec.get("po_line"),
              "sku": spec["sku"], "asin": spec.get("asin")},
        operator_id=spec.get("operator_id"), inputs=inputs_list, payload=payload,
        latency_ms=int((time.time() - t0) * 1000),
        confidence=round(sum(confs) / len(confs), 2) if confs else None)
    return build_output(record)


from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

app = FastAPI(title="Receiving Manager")

@app.get("/health")
def health():
    return {"status":"ok","stage":STAGE,"version":VERSION,"contract_version":"1.0",
            "provider_configured":bool(CFG.gemini_api_key)}

@app.post("/run")
async def run(request: Request):
    try:
        body = await request.json()
    except (ValueError, UnicodeError):
        return JSONResponse({"error":"invalid_json"},status_code=422)
    try:
        return await run_in_threadpool(handle,body)
    except Rejected as exc:
        return JSONResponse({"error":exc.code},status_code=exc.status)
    except LookupError:
        return JSONResponse({"error":"subject_not_authorized"},status_code=404)
    except Exception:
        return JSONResponse({"error":"receiving_unavailable"},status_code=503)
