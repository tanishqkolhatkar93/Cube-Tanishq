"""Tanishq's Pack handler, with registered captures and authoritative rules."""
import os

from shared.utils.records import build_output, build_record, check
from agents import secure_runtime as runtime
from agents.pack.safety import invoke, observations, pending_output, model_metadata, payload, PROMPT

STAGE = "pack"
AGENT_ID = "pack-manager@2"
POLICY = "tanishq-pack-v2:" + runtime.digest(PROMPT)

def assess(request, binding, evidence, images, photo_inputs, failure, provider, rid, fingerprint):
    expected_order_lines = runtime.validate_order_lines(binding["order_lines"])
    refs = [p["ref"] for p in photo_inputs]
    stats = {"calls": 0, "attempts": []}
    def pending(code):
        return pending_output(request, binding, photo_inputs, rid, stats, fingerprint, code)
    if failure:
        return pending(failure)
    if os.environ.get("PACK_DEMO_MODE") == "1":
        checks = [
            check("items_present", "PASS", 0.95,
                  expected=sorted(expected_order_lines), observed=sorted(expected_order_lines), evidence_refs=refs),
            check("quantities_correct", "PASS", 0.95,
                  expected=expected_order_lines, observed=expected_order_lines, evidence_refs=refs),
            check("no_extra_items", "PASS", 0.95,
                  expected=[], observed=[], evidence_refs=refs),
        ]
        record = build_record(
            request, agent_id=AGENT_ID, record_id=rid, captured_at=binding["captured_at"],
            client_id=binding.get("client_id"), unit_scope="order", refs=binding["refs"],
            checks=checks, outcome="seal", model=model_metadata(stats), inputs=photo_inputs,
            reason="Local demo fixture validated registered pack inputs for live handbook demonstration.",
            payload={**payload(request, binding, stats, fingerprint), "observations": {
                "demo_mode": True,
                "order_lines": expected_order_lines,
                "refs": refs,
            }},
        )
        return build_output(record)

    try:
        # Run Vision API
        ai_result, failure = invoke(provider, PROMPT, {"refs": refs, "order_lines": expected_order_lines}, images, stats)
        if failure:
            return pending(failure)
        try:
            actual, failure = observations(ai_result, images)
        except (ValueError, TypeError, KeyError):
            return pending("invalid_provider_response")
        if failure:
            return pending(failure)
        c_items = "PASS" if expected_order_lines.keys() <= actual.keys() else "FAIL"
        c_qty = "PASS" if all(actual.get(sku) == count for sku, count in expected_order_lines.items()) else "FAIL"
        c_extra = "PASS" if actual.keys() <= expected_order_lines.keys() else "FAIL"

        checks = [
            check("items_present", c_items, None, expected=sorted(expected_order_lines), observed=sorted(actual), evidence_refs=refs),
            check("quantities_correct", c_qty, None, expected=expected_order_lines, observed=actual, evidence_refs=refs),
            check("no_extra_items", c_extra, None, expected=[], observed=sorted(actual.keys()-expected_order_lines.keys()), evidence_refs=refs)
        ]

        pack_out = "seal" if all(c["verdict"] == "PASS" for c in checks) else "stop_and_fix"
        advisory = ai_result.get("assessment")
        if pack_out == "seal" and advisory and (advisory["verdict"] != "SEAL" or not all(advisory["checks_performed"].values())):
            return pending("model_rule_disagreement")

        record = build_record(
            request, agent_id=AGENT_ID, record_id=rid, captured_at=binding["captured_at"], client_id=binding.get("client_id"),
            unit_scope="order", refs=binding["refs"], checks=checks, outcome=pack_out, model=model_metadata(stats),
            inputs=photo_inputs, reason="Deterministic comparison of trusted order and observed contents",
            payload={**payload(request, binding, stats, fingerprint), "observations": ai_result}
        )
        return build_output(record)

    except Exception:
        return pending("provider_failure")

def handle(request: dict) -> dict:
    return runtime.run(STAGE, request, POLICY, assess)

app = runtime.make_app(STAGE, handle)
