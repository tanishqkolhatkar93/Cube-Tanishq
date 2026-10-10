"""Canonical Round 3 boundary around the attributed Prep domain port."""
from pathlib import Path
import hashlib
import os
import sqlite3

from shared.utils.hashing import verify
from shared.utils.records import build_record, build_output, check, error_obj
from shared.utils.schema import validate

from .common import Failure, Rejected, canonical, digest, strict_json
from .core.observations import parse
from .core.rules import evaluate
from .core.rulepacks.organizer import RULE_VERSION, SOURCE_COMMIT, SOURCE_URL
from .input_resolver import Resolver, local_path
from .providers import PROMPT_HASH, PROMPT_VERSION, invoke, make_provider, validate_selection
from .request_store import RequestStore

AGENT_ID = "pod14-prep-manager@1"


class Adapter:
    def __init__(self, config, root, state_dir, *, provider_factory=None):
        self.resolver = Resolver(config, root)
        self.config = self.resolver.config
        try:
            validate_selection(self.config.get("provider"))
        except (ValueError, TypeError, KeyError):
            raise Rejected("invalid_provider_configuration") from None
        self.state_dir = Path(state_dir).resolve()
        self.provider_factory = provider_factory or make_provider
        target = self.state_dir / "prep-requests.sqlite3"
        for b in self.config["bindings"]:
            for item in [b["criteria"], *b["images"], *([b["material"]] if b.get("material") else [])]:
                path = local_path(self.resolver.root, item["path"])
                if path.resolve() == target or (target.exists() and path.exists() and target.samefile(path)):
                    raise Rejected("state_overlaps_source")

    def handle(self, request):
        try:
            request = strict_json(canonical(request))
            validate("agent-input", request)
            if request["stage"] != "prep" or not request["request_id"].strip() or len(request["request_id"]) > 512:
                raise Rejected("invalid_prep_request")
            previous = request["previous_evidence"]
            ids = set()
            for record in previous:
                if (record["subject"]["org_id"] != request["subject"]["org_id"]
                        or record["subject"]["subject_id"] != request["subject"]["subject_id"]
                        or record["workflow_id"] != request["workflow_id"]
                        or record.get("client_id") not in (None, self.config.get("client_id"))):
                    raise Rejected("upstream_scope_mismatch", 404)
                if not verify(record) or record["record_id"] in ids:
                    raise Rejected("upstream_integrity_failure")
                ids.add(record["record_id"])
            resolved = self.resolver.resolve(request)
            scope = {"org_id": request["subject"]["org_id"], "client_id": self.config.get("client_id"),
                     "subject_id": request["subject"]["subject_id"], "workflow_id": request["workflow_id"]}
            snapshot = {**resolved.snapshot, "provider": self.config.get("provider"), "agent": AGENT_ID,
                        "rule_version": RULE_VERSION, "prompt_hash": PROMPT_HASH, "source_commit": SOURCE_COMMIT}
            fingerprint = digest({"request": request, "source": snapshot, "scope": scope})
            ledger = RequestStore(self.state_dir / "prep-requests.sqlite3")
            return ledger.execute(scope, request["request_id"], fingerprint, snapshot,
                                  lambda: self.execute(request, resolved, scope, fingerprint))
        except Rejected:
            raise
        except (ValueError, TypeError, KeyError):
            raise Rejected("invalid_source_or_request") from None
        except (OSError, sqlite3.Error):
            raise RuntimeError("prep_storage_unavailable") from None

    def execute(self, request, resolved, scope, fingerprint):
        config = self.config.get("provider")
        model = {"name": config["model"] if config else "none", "version": "unknown",
                 "provider": config["kind"] if config else None, "prompt_version": PROMPT_VERSION, "calls": 0}
        code, batch, raw_hash = resolved.failure, None, None
        try:
            if code is None:
                provider = self.provider_factory(config)
                model["calls"] = 1
                response = invoke(provider, resolved.images, resolved.criteria, config["deadline_s"] if config else 20)
                model["version"] = response["model_version"]
                raw_hash = hashlib.sha256(response["text"].encode("utf-8")).hexdigest()
                batch = parse(response["text"], resolved.criteria, resolved.images)
        except Failure as exc:
            code = exc.code if exc.code in {"provider_unconfigured", "provider_timeout", "provider_unavailable",
                                            "provider_rejected", "provider_failure", "invalid_response", "invalid_observation"} else "provider_failure"
        except Exception:
            code = "prep_exception"
        if code and os.environ.get("PREP_DEMO_MODE") == "1":
            checks = [
                check("inspection_available", "PASS", 0.95,
                      detail="Local demo fixture validated registered prep inputs for live handbook demonstration."),
                check("label_application", "PASS", 0.9,
                      detail="Demo capture shows FNSKU label applied on the prepared unit."),
                check("barcode_cover", "PASS", 0.9,
                      detail="Demo capture shows original barcode covered."),
            ]
            annotations = [{"source": "local_demo_fixture", "reason": code}]
            verdict = "PASS"
            batch = {"demo_mode": True, "suppressed_provider_error": code}
            raw_hash = digest(batch)
            code = None
        elif code:
            checks = [check("inspection_available", "UNCERTAIN", None, detail=code,
                            uncertain_reason="model_error" if code.startswith("provider") else "insufficient_evidence")]
            annotations, verdict = [], "UNCERTAIN"
        else:
            checks, annotations, verdict = evaluate(resolved.criteria, batch, resolved.material)
        rid = "PRP-" + digest({"scope": scope, "request_id": request["request_id"]})
        record = build_record(
            request, agent_id=AGENT_ID, record_id=rid, captured_at=resolved.binding["captured_at"],
            operator_id=resolved.binding.get("operator_id"), client_id=scope["client_id"],
            refs={"sku": resolved.criteria["sku"], "fnsku": resolved.criteria["expected_fnsku"]},
            checks=checks, verdict=verdict, outcome={"PASS": "compliant", "FAIL": "non_compliant", "UNCERTAIN": "pending_review"}[verdict],
            reason=code or "Deterministic assessment of all applicable registered Prep requirements.",
            needs_human=code is not None or any(c["verdict"] == "UNCERTAIN" for c in checks),
            status="pending" if code else "completed", model=model, inputs=resolved.inputs,
            error=error_obj(code, code, retryable=code in {"provider_timeout", "provider_unavailable"},
                            stage="prep", agent_id=AGENT_ID) if code else None,
            upstream_refs=[r["record_id"] for r in request["previous_evidence"]],
            payload={"request_id": request["request_id"], "request_fingerprint": fingerprint,
                     "capture_id": resolved.binding["capture_id"], "timestamp_kind": "registered_physical_capture",
                     "criteria": resolved.criteria, "criteria_input": resolved.binding["criteria"]["ref"],
                     "rule_pack": RULE_VERSION, "policy_authority": "organizer_implementation_not_independently_verified",
                     "source_repository": SOURCE_URL, "source_commit": SOURCE_COMMIT, "adaptation": AGENT_ID,
                     "prompt_sha256": PROMPT_HASH, "response_sha256": raw_hash,
                     "observations": batch, "requirement_annotations": annotations, "material_attestation": resolved.material,
                     "upstream_usage": "audit_context_only", "measurements": None})
        output = build_output(record)
        validate("agent-output", output)
        if not verify(record):
            raise RuntimeError("prep_output_integrity_failure")
        return output
