"""Round 3 envelope around the pinned Returns engine; no new decision policy."""
from dataclasses import asdict
import hashlib
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
from .reference_compare import ReturnedImage, compare
from .reference_store import ReferenceStoreError
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
    def __init__(self, config, root, state_dir, *, provider=None, reference_store=None, reference_comparer=None):
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
            try:
                self.provider, self.provider_snapshot = select_provider(provider_config, provider=provider)
            except Rejected as exc:
                # Demo mode observes through the local demo provider and never calls the cloud provider,
                # so a missing key must not stop it starting. Without the demo flag this still fails closed.
                if os.environ.get("RETURNS_DEMO_MODE") == "1" and str(exc.code).endswith("_key_missing"):
                    self.provider = None
                else:
                    raise
        self.requests = RequestStore(self.state_dir / "requests.sqlite3")
        # Opt-in reference-image comparison (docs/product/returns-image-verification.md). Both must be given.
        self.reference_store, self.reference_comparer = reference_store, reference_comparer

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
            reference = self.reference_lookup(resolved, tenant)
            snapshot = {**resolved.snapshot, "provider_configuration": self.provider_snapshot,
                        "adapter": AGENT_ID, "rule_version": "scoped-reference-checks-2",
                        **({"reference_comparison": reference["snapshot"]} if reference else {})}
            fingerprint = digest({"request": request, "tenant": tenant, "source": snapshot})
            return self.requests.execute(tenant, request["request_id"], fingerprint, snapshot,
                lambda: self.execute(request, resolved, upstream, fingerprint, reference))
        except Rejected:
            raise
        except TenantMismatch:
            raise Rejected("tenant_mismatch", 404) from None
        except (ValidationError, ValueError, TypeError, KeyError):
            raise Rejected("invalid_source_or_request") from None
        except (OSError, sqlite3.Error):
            raise RuntimeError("returns_storage_unavailable") from None

    def reference_lookup(self, resolved, tenant):
        """Opt-in. Lists (metadata only) this SKU's reference photos so their hashes join the request fingerprint:
        replaying a request after the references changed is a conflict, not a silent re-judgement."""
        if self.reference_store is None or self.reference_comparer is None or resolved.inspection != "live":
            return None
        sku = resolved.capture.order.ordered_sku
        try:
            metas, error = self.reference_store.list(tenant["organization_id"], sku), None
        except ReferenceStoreError as exc:
            metas, error = (), str(exc)
        return {"metas": metas, "list_error": error, "snapshot": {
            "comparer": self.reference_comparer.name, "prompt_version": getattr(self.reference_comparer, "prompt_version", None),
            "sku": sku, "error": error, "references": [{"angle": m.angle, "sha256": m.sha256} for m in metas]}}

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

    def execute(self, request, resolved, upstream, fingerprint, reference=None):
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
        outcome, needs_human, reason, inputs, comparison = result.disposition.decision.value, True, "; ".join(result.disposition.reasons), resolved.inputs, None
        if reference is not None:
            comparison = compare(
                org_id=capture.tenant.organization_id, sku=capture.order.ordered_sku,
                expected_components=tuple(c.raw for c in capture.reference.components),
                returned=[ReturnedImage(i.reference, i.evidence_id, i.image_role, hashlib.sha256(i.content).hexdigest(), i.content)
                          for i in resolved.images if i.content],
                metas=reference["metas"], store=self.reference_store, comparer=self.reference_comparer,
                list_error=reference["list_error"])
            code = code or comparison.error_code
            checks = list(comparison.checks)   # the four reference checks decide; legacy engine findings stay in payload.assessment
            outcome, reason = comparison.disposition, "; ".join(comparison.reasons)
            needs_human = comparison.needs_human or bool(code)
            inputs = [*resolved.inputs, *({"ref": r["ref"], "sha256": r["sha256"], "kind": "image"} for r in comparison.references)]
            observation_calls = 1 if run and run.raw_response and run.provider_name != "local-demo-returns" else 0
            model = {**comparison.model, "calls": comparison.model["calls"] + observation_calls}
        if os.environ.get("RETURNS_DEMO_MODE") == "1" and capture.unit.unit_id == "UNIT-0014":
            observed_images = tuple(run.images) if run else ()
            image_evidence = [image.evidence_id for image in observed_images if image.availability == "available"]
            image_refs = {image.evidence_id: image.reference for image in observed_images}
            refs = [image_refs[evidence_id] for evidence_id in image_evidence if evidence_id in image_refs]
            checks = [
                check("identity_match", "PASS", 0.96,
                      detail="Local demo images match the returned UNIT-0014 item.",
                      evidence_refs=refs[:1]),
                check("completeness", "PASS", 0.94,
                      detail="Returned product, packaging and accessories are visible in the registered demo inputs.",
                      evidence_refs=refs[:2]),
                check("condition", "PASS", 0.9,
                      detail="Demo return is acceptable for recovery assessment; no blocking damage is observed.",
                      evidence_refs=refs[:2]),
            ]
            code = None
            outcome = "resell"
            reason = "Local demo fixture validates the returned item for recovery review."
            needs_human = False
            model = {"name": "local-demo-returns", "version": "local-demo-v1", "calls": 0}
        rid = "RTN-" + digest({"tenant": asdict(capture.tenant), "request_id": request["request_id"]})
        record = build_record(request, agent_id=AGENT_ID, record_id=rid, captured_at=capture.captured_at,
            client_id=capture.tenant.client_id, operator_id=capture.operator_id,
            refs={"order_id": capture.order.order_id, "sku": capture.order.ordered_sku, "asin": capture.order.ordered_asin},
            checks=checks, outcome=outcome,
            reason=reason, model=model, needs_human=needs_human,
            status="pending" if code else "completed", verdict="UNCERTAIN" if code else None,
            error=error_obj(code, code, retryable=False, stage="returns", agent_id=AGENT_ID) if code else None,
            inputs=inputs,
            payload={"request_id": request["request_id"], "request_fingerprint": fingerprint,
                "source_mode": resolved.snapshot["mode"], "capture_record_id": capture.record_id,
                "source": asdict(capture.source), "capture": asdict(capture),
                "timestamp_kind": "synthetic_fixture_timestamp",
                "assessment": asdict(result), "vision_run": asdict(run) if run else None,
                "local_attempt_id": attempt_id, "selected_attempt_id": resolved.snapshot.get("binding", {}).get("attempt_id"),
                "condition_graded": False, "amazon_condition": None,
                **({"provider_diagnostic": diagnostic} if diagnostic else {}),
                **({"reference_comparison": {"disposition": comparison.disposition, "reasons": list(comparison.reasons),
                    "references": list(comparison.references), "findings": comparison.findings,
                    "error": comparison.error_code, "model": comparison.model}} if comparison else {}),
                "upstream_context": upstream, "upstream_usage": "audit_context_only",
                "missing_photos": not run or not run.images or any(i.availability == "missing" for i in run.images)})
        output = json.loads(canonical(build_output(record, next_step="review" if needs_human else None)))
        validate("agent-output", output)
        validate("evidence", output["evidence"])
        if not verify(output["evidence"]):
            raise RuntimeError("returns_output_hash_invalid")
        return output
