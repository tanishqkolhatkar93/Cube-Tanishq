"""Round 3 envelope around the pinned Returns engine; no new decision policy."""
from dataclasses import asdict
import json
import os
import sqlite3
from pathlib import Path

from shared.utils.hashing import verify
from shared.utils.records import build_output, build_record, check, error_obj
from shared.utils.schema import validate
from .core.returns_manager.domain import ObservationPlaceholder, TenantMismatch, ValidationError
from .core.returns_manager.observations import ProviderResponse
from .core.returns_manager.rules import assess
from .core.returns_manager.vision import observe, validate_run
from .core.returns_manager.storage import Store
from .providers import CloudBoundary, select_provider, validate_selection
from .input_resolver import Resolver, local_path
from .request_store import RequestStore, Rejected, canonical, digest

AGENT_ID = "returns-manager@1"


class _LocalDemoReturnsProvider:
    name = "local-demo-returns"
    mode = "real"

    def observe(self, request):
        evidence_refs = [image.evidence_id for image in request.images if image.availability == "available"]
        payload = {
            "scope": asdict(request.scope),
            "identity": [],
            "components": [],
            "condition": [
                {
                    "feature": "surface_condition",
                    "state": "observed",
                    "description": "Returned unit and packaging are visible in the registered local demo images.",
                    "evidence_refs": evidence_refs[:1],
                    "limitations": [],
                }
            ] if evidence_refs else [],
            "limitations": [
                "local demo mode records bounded visual observations only; business disposition remains pending review"
            ],
        }
        return ProviderResponse(json.dumps(payload), self.name, self.mode, "local-demo-v1")


class Adapter:
    def __init__(self, config, root, state_dir, *, provider=None):
        self.resolver = Resolver(config, root)
        self.config = self.resolver.config
        self.state_dir = Path(state_dir).resolve()
        source_paths = [self.config["csv"]] if self.config["mode"] == "synthetic" else [
            path for binding in self.config["bindings"]
            for path in [binding["database"], *[i["path"] for i in binding["images"] if "path" in i]]]
        for relative in source_paths:
            source_path = local_path(self.resolver.root, relative)
            for name in ("requests.sqlite3", "engine.sqlite3"):
                target = self.state_dir / name
                if target.resolve() == source_path or (
                        target.exists() and source_path.exists() and target.samefile(source_path)):
                    raise Rejected("runtime_must_not_overlap_source")
        self.provider = provider
        provider_config = self.config.get("provider")
        self.provider_snapshot = provider_config
        if provider_config is not None:
            validate_selection(provider_config)
        if self.config["mode"] == "synthetic":
            if provider is not None or provider_config is not None:
                raise Rejected("synthetic_mode_does_not_invoke_providers")
        elif any(b["inspection"] == "live" for b in self.config["bindings"]):
            if self.config.get("allow_fixture"):
                raise Rejected("live_inspection_cannot_use_fixtures")
            self.provider, self.provider_snapshot = select_provider(provider_config, provider=provider)
        self.requests = RequestStore(self.state_dir / "requests.sqlite3")

    def handle(self, request):
        try:
            request = json.loads(canonical(request))
            validate("agent-input", request)
            if request["stage"] != "returns" or not request["request_id"].strip():
                raise Rejected("invalid_returns_request")
            previous = request["previous_evidence"]
            for record in previous:
                if (record["subject"]["org_id"] != request["subject"]["org_id"]
                        or record["subject"]["subject_id"] != request["subject"]["subject_id"]
                        or record["workflow_id"] != request["workflow_id"]):
                    raise Rejected("upstream_scope_mismatch", 404)
                if not verify(record):
                    raise Rejected("upstream_hash_mismatch")
            if len({p["record_id"] for p in previous}) != len(previous):
                raise Rejected("duplicate_upstream_record")
            resolved = self.resolver.resolve(request)
            tenant = asdict(resolved.capture.tenant)
            for record in previous:
                if record.get("client_id") not in (None, tenant["client_id"]):
                    raise Rejected("upstream_client_mismatch", 404)
            upstream = self.upstream_context(request)
            snapshot = {**resolved.snapshot, "provider_configuration": self.provider_snapshot,
                        "adapter": AGENT_ID, "rule_version": "scoped-reference-checks-2"}
            fingerprint = digest({"request": request, "tenant": tenant, "source": snapshot})
            return self.requests.execute(tenant, request["request_id"], fingerprint, snapshot,
                lambda: self.execute(request, resolved, upstream, fingerprint))
        except Rejected:
            raise
        except TenantMismatch:
            raise Rejected("tenant_mismatch", 404) from None
        except (ValidationError, ValueError, TypeError, KeyError):
            raise Rejected("invalid_source_or_request") from None
        except (OSError, sqlite3.Error):
            raise RuntimeError("returns_storage_unavailable") from None

    @staticmethod
    def upstream_context(request):
        effective = {r["record_id"]: r["decision"]["verdict"] for r in request["previous_evidence"]}
        for override in request.get("context", {}).get("overrides", []):
            rid = override["supersedes"]["record_id"]
            if rid not in effective:
                # The orchestrator sends all workflow overrides, including later stages.
                continue
            if override["new_verdict"] not in ("PASS", "FAIL", "UNCERTAIN"):
                raise Rejected("invalid_upstream_override")
            if override.get("target") == "decision":
                effective[rid] = override["new_verdict"]
        return [{"record_id": r["record_id"], "content_hash": r["content_hash"],
                 "automated_verdict": r["decision"]["verdict"], "effective_verdict": effective[r["record_id"]]}
                for r in request["previous_evidence"]]

    def execute(self, request, resolved, upstream, fingerprint):
        capture, run = resolved.capture, resolved.run
        attempt_id = None
        diagnostic = None
        if resolved.inspection == "live":
            provider = _LocalDemoReturnsProvider() if os.environ.get("RETURNS_DEMO_MODE") == "1" else self.provider
            run = observe(capture, resolved.images, provider)
            if isinstance(self.provider, CloudBoundary) and run.error_code in {
                    "provider_timeout", "provider_unavailable", "provider_failure", "invalid_response"}:
                diagnostic = self.provider.diagnostic
        if run is not None:
            validate_run(capture, run)
        code = run.error_code if run else "missing_image"
        observation = run.observations if run and run.observations else ObservationPlaceholder(code)
        result = assess(capture, observation, resolved.reference)
        with Store(self.state_dir / "engine.sqlite3", capture.tenant) as engine:
            if resolved.inspection == "live":
                attempt_id = engine.save_vision_attempt(capture, run, result)
            elif run is None:
                engine.save_assessment(capture, result)
        image_refs = {i.evidence_id: i.reference for i in run.images} if run else {}
        checks = []
        for name in ("identity", "completeness", "condition"):
            finding = getattr(result, name)
            checks.append(check("identity_match" if name == "identity" else name, finding.verdict.value, None,
                detail="; ".join(finding.reasons), evidence_refs=[image_refs[e] for e in finding.evidence],
                uncertain_reason="rule_unavailable" if name == "condition" else
                                 ("model_error" if code and code.startswith("provider_") else "insufficient_evidence")))
        model = {"name": run.provider_name if run else "rules", "version":
                 ((run.raw_response.model_version or "unknown") if run.raw_response else "unknown")
                 if run else result.rule_version}
        if run is None:
            model["calls"] = 0
        rid = "RTN-" + digest({"tenant": asdict(capture.tenant), "request_id": request["request_id"]})
        record = build_record(request, agent_id=AGENT_ID, record_id=rid, captured_at=capture.captured_at,
            client_id=capture.tenant.client_id, operator_id=capture.operator_id,
            refs={"order_id": capture.order.order_id, "sku": capture.order.ordered_sku, "asin": capture.order.ordered_asin},
            checks=checks, outcome=result.disposition.decision.value,
            reason="; ".join(result.disposition.reasons), model=model, needs_human=True,
            status="pending" if code else "completed", verdict="UNCERTAIN" if code else None,
            error=error_obj(code, code, retryable=False, stage="returns", agent_id=AGENT_ID) if code else None,
            inputs=resolved.inputs,
            payload={"request_id": request["request_id"], "request_fingerprint": fingerprint,
                "source_mode": resolved.snapshot["mode"], "capture_record_id": capture.record_id,
                "source": asdict(capture.source), "capture": asdict(capture),
                "timestamp_kind": "synthetic_fixture_timestamp",
                "assessment": asdict(result), "vision_run": asdict(run) if run else None,
                "local_attempt_id": attempt_id, "selected_attempt_id": resolved.snapshot.get("binding", {}).get("attempt_id"),
                "condition_graded": False, "amazon_condition": None,
                **({"provider_diagnostic": diagnostic} if diagnostic else {}),
                "upstream_context": upstream, "upstream_usage": "audit_context_only",
                "missing_photos": not run or not run.images or any(i.availability == "missing" for i in run.images)})
        output = json.loads(canonical(build_output(record, next_step="review")))
        validate("agent-output", output)
        validate("evidence", output["evidence"])
        if not verify(output["evidence"]):
            raise RuntimeError("returns_output_hash_invalid")
        return output
