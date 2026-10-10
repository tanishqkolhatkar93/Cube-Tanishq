"""Internal Pack/Recovery capability boundary, reusing the integrated Prep ledger.

Configuration is operator-owned, never request-owned. Deploy behind authenticated
orchestration; an org_id in JSON is not a credential. No sample-data fallback.
"""
import copy
import hashlib
import io
import os
import sqlite3
import time
from pathlib import Path
import warnings

from PIL import Image
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from agents.prep.common import Rejected, canonical, digest, strict_json, fields, timestamp, text
from agents.prep.input_resolver import registered_bytes, safe_ref, local_path
from agents.prep.request_store import RequestStore
from shared.utils.hashing import verify
from shared.utils.schema import validate
from shared.utils.records import build_record, build_output, error_obj, rollup


def validate_order_lines(value):
    if not isinstance(value,dict) or not value:raise ValueError('order manifest required')
    for sku,count in value.items():
        text(sku)
        if type(count) is not int or not 1<=count<=100_000:raise ValueError('invalid manifest count')
    return value


def configuration(stage):
    name = os.environ.get(stage.upper() + '_CONFIG')
    state = os.environ.get(stage.upper() + '_STATE_DIR')
    if not name or not state:
        raise Rejected('configuration_required', 503)
    try:
        path = Path(name).resolve()
        config = strict_json(path.read_text(encoding='utf-8'))
        canonical(config)  # Reject overflow-to-infinity JSON numbers too.
        fields(config, ('version', 'bindings'), ('provider',))
        if type(config['version']) is not int or config['version'] != 1 or not isinstance(config['bindings'], list):
            raise ValueError()
        keys = set()
        for b in config['bindings']:
            fields(b, ('org_id', 'subject_id', 'workflow_id', 'captured_at', 'files', 'trusted_agents'),
                   ('refs', 'order_lines', 'trusted_overrides', 'fee_policies', 'client_id'))
            key = tuple(text(b[k]) for k in ('org_id', 'subject_id', 'workflow_id'))
            timestamp(b['captured_at'])
            if key in keys or not isinstance(b['files'], list) or len(b['files']) > 8:
                raise ValueError()
            keys.add(key)
            if not isinstance(b['trusted_agents'], dict):
                raise ValueError()
            for upstream_stage, agents in b['trusted_agents'].items():
                if upstream_stage not in ('receiving','prep','pack','returns') or not isinstance(agents,list):
                    raise ValueError()
                for agent in agents:text(agent)
            if not isinstance(b.get('refs',{}),dict) or not isinstance(b.get('trusted_overrides',[]),list):
                raise ValueError()
            if b.get('client_id') is not None:text(b['client_id'])
            for item in b['files']:
                fields(item, ('ref','path','sha256','kind'))
                safe_ref(item['ref']);safe_ref(item['path'])
                if item['kind'] != ('image' if stage=='pack' else 'document'):
                    raise ValueError()
            if stage=='pack':
                validate_order_lines(b.get('order_lines'))
                text(b.get('refs',{}).get('order_id'))
        selection = config.get('provider')
        if selection is not None:
            fields(selection, ('model', 'api_key_env', 'deadline_s'))
            text(selection['model']); text(selection['api_key_env'])
            if type(selection['deadline_s']) not in (int, float) or not 0.05 <= selection['deadline_s'] <= 20:
                raise ValueError()
        return config, path.parent, Path(state).resolve()
    except Rejected:
        raise
    except (ValueError, KeyError, TypeError, OSError):
        raise Rejected('invalid_configuration', 503) from None


def request_boundary(request, stage):
    try:
        request = strict_json(canonical(request))
        validate('agent-input', request)
        if request['stage'] != stage:
            raise ValueError()
        for value in (request['request_id'], request['workflow_id'], *[request['subject'][k] for k in ('org_id', 'subject_id')]):
            text(value)
        if stage == 'pack' and request['subject'].get('route') != 'mfn':
            raise ValueError()
        return request
    except (ValueError, KeyError, TypeError):
        raise Rejected('invalid_request', 422) from None


def upstream(request, binding, stage):
    allowed = {'pack': {'receiving'}, 'recovery': {'receiving', 'prep', 'pack', 'returns'}}[stage]
    scopes = {'receiving': {'unit', 'po_line'}, 'prep': {'unit'}, 'pack': {'order'}, 'returns': {'unit'}}
    seen, by_stage = {}, set()
    for record in request['previous_evidence']:
        subject = record['subject']
        if (record['stage'] not in allowed or record['stage'] in by_stage
                or subject['org_id'] != binding['org_id'] or subject['subject_id'] != binding['subject_id']
                or subject.get('unit_id') not in (None, binding['subject_id'])
                or record['workflow_id'] != binding['workflow_id']
                or record.get('client_id') not in (None, binding.get('client_id'))
                or subject.get('unit_scope') not in scopes.get(record['stage'], set())):
            raise Rejected('upstream_scope_mismatch')
        if record['agent_id'] not in binding['trusted_agents'].get(record['stage'], []):
            raise Rejected('untrusted_upstream_agent')
        if not verify(record) or record['record_id'] in seen or record.get('overrides'):
            raise Rejected('upstream_integrity_failure')
        prefix={'receiving':'RCV-','prep':'PRP-','pack':'PCK-','returns':'RTN-'}[record['stage']]
        if not record['record_id'].startswith(prefix) or len({c['check_key'] for c in record['checks']})!=len(record['checks']):
            raise Rejected('upstream_semantic_conflict')
        if record['status']=='completed':
            if rollup(record['checks'])!=record['decision']['verdict']:
                raise Rejected('upstream_semantic_conflict')
            if record['stage']=='receiving' and subject.get('unit_scope')!='po_line':
                raise Rejected('upstream_scope_mismatch')
        elif record['decision']['verdict']!='UNCERTAIN':
            raise Rejected('upstream_semantic_conflict')
        refs = subject.get('refs', {})
        if any(k in refs and refs[k] != v for k, v in binding.get('refs', {}).items()):
            raise Rejected('upstream_reference_conflict')
        if not set(record['upstream_refs']) <= seen.keys():
            raise Rejected('unknown_upstream_reference')
        evidence_refs = {i['ref'] for i in record.get('inputs', [])} | set(record['upstream_refs'])
        if any(not set(c.get('evidence_refs', [])) <= evidence_refs for c in record['checks']):
            raise Rejected('invalid_check_reference')
        seen[record['record_id']] = record
        by_stage.add(record['stage'])
    # Overrides are excluded from evidence hashes. Require exact operator-owned
    # authorization and validate the chain instead of trusting request context.
    authorized = binding.get('trusted_overrides', [])
    effective = {rid: r['decision']['verdict'] for rid, r in seen.items()}
    latest, override_ids = {}, set()
    for override in request.get('context', {}).get('overrides', []):
        if override not in authorized:
            raise Rejected('unauthorized_override')
        try:
            rid = override['supersedes']['record_id']
            if (rid not in seen or override['target'] != 'decision' or override['override_id'] in override_ids
                    or override['new_verdict'] not in {'PASS', 'FAIL', 'UNCERTAIN'}
                    or override['original_verdict'] != seen[rid]['decision']['verdict']
                    or override['previous_verdict'] != effective[rid]
                    or override['supersedes']['override_id'] != latest.get(rid)):
                raise ValueError()
            text(override['actor']); text(override['reason']); timestamp(override['at'])
            effective[rid] = override['new_verdict']
            latest[rid] = override['override_id']; override_ids.add(override['override_id'])
        except (ValueError, KeyError, TypeError):
            raise Rejected('invalid_override_chain') from None
    return [{**copy.deepcopy(r), 'effective_verdict': effective[rid]} for rid, r in seen.items()]


def resolve(request, stage):
    config, root, state = configuration(stage)
    matches = [b for b in config['bindings'] if (b['org_id'], b['subject_id'], b['workflow_id']) ==
               (request['subject']['org_id'], request['subject']['subject_id'], request['workflow_id'])]
    if len(matches) != 1:
        raise Rejected('source_not_found', 404)
    binding = matches[0]
    evidence = upstream(request, binding, stage)
    expected, paths, loaded, failure = {}, set(), [], None
    for item in binding['files']:
        fields(item, ('ref', 'path', 'sha256', 'kind'))
        safe_ref(item['ref'])
        path = local_path(root, item['path'])
        if path.is_relative_to(state) or path in paths or item['ref'] in expected:
            raise Rejected('ambiguous_or_state_source')
        paths.add(path)
        expected[item['ref']] = {k: item[k] for k in ('ref', 'sha256', 'kind')}
    asserted = {}
    for item in request.get('inputs', []):
        ref = safe_ref(item['ref'])
        if ref in asserted or ref not in expected:
            raise Rejected('unregistered_input')
        if item.get('sha256') != expected[ref]['sha256'] or item.get('kind') != expected[ref]['kind']:
            raise Rejected('input_assertion_mismatch')
        asserted[ref] = item
    if not expected or asserted.keys() != expected.keys():
        failure = 'required_inputs_missing'
    for item in binding['files']:
        if item['ref'] not in asserted:
            continue
        try:
            raw = registered_bytes(root, {k: item[k] for k in ('ref', 'path', 'sha256')},
                                   10_000_000 if stage=='pack' else 1_000_000)
        except FileNotFoundError:
            failure = failure or 'registered_input_missing'
            continue
        entry = {**expected[item['ref']], 'bytes': raw}
        if stage == 'pack':
            if item['kind'] != 'image':
                raise Rejected('image_required')
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('error', Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(raw)) as image:
                        fmt = image.format
                        if fmt not in ('JPEG', 'PNG', 'WEBP') or image.width * image.height > 20_000_000:
                            raise ValueError()
                        image.verify()
                entry['media_type'] = {'JPEG': 'image/jpeg', 'PNG': 'image/png', 'WEBP': 'image/webp'}[fmt]
            except (ValueError, OSError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError):
                failure = failure or 'invalid_image'
        elif item['kind'] != 'document':
            raise Rejected('report_document_required')
        loaded.append(entry)
    return config, state, binding, evidence, loaded, list(expected.values()), failure


def run(stage, request, policy_version, execute):
    started = time.monotonic()
    request = request_boundary(request, stage)
    try:
        config, state, binding, evidence, loaded, inputs, failure = resolve(request, stage)
        snapshot = {'binding': binding, 'provider': config.get('provider'), 'policy': policy_version,
                    'loaded': [i['sha256'] for i in loaded], 'failure': failure}
        scope = {k: binding[k] for k in ('org_id', 'subject_id', 'workflow_id')}
        fingerprint = digest({'request': request, 'snapshot': snapshot})
        rid = {'pack': 'PCK-', 'recovery': 'RCY-'}[stage] + digest({'scope': scope, 'request_id': request['request_id']})
        deadline = started + (config.get('provider') or {}).get('deadline_s',20)
        def produce():
            remaining = deadline-time.monotonic()
            selection = config.get('provider')
            if selection:
                selection = {**selection,'deadline_s':max(0,remaining)}
            result = execute(request,binding,evidence,loaded,inputs,
                failure or ('processing_deadline_exceeded' if remaining<=0 else None),selection,rid,fingerprint)
            if result['status']=='completed' and time.monotonic()>deadline:
                model=result['model']
                stats={**model,'model':model['name'],'attempts':result['evidence']['payload'].get('provider_attempts',[])}
                return output(stage,request,binding,inputs,rid,fingerprint,[],
                    {'claimable_usd':0,'charges':[],'discarded_late_result':True,
                     'provider_usage':result['evidence']['payload'].get('provider_usage',{})},stats,'processing_deadline_exceeded')
            return result
        return RequestStore(state / (stage + '.sqlite3')).execute(scope, request['request_id'], fingerprint, snapshot,
                                                                 produce)
    except Rejected:
        raise
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise Rejected('invalid_registered_source') from None
    except (OSError,sqlite3.Error):
        raise Rejected('storage_unavailable',503) from None


def output(stage, request, binding, inputs, rid, fingerprint, checks, payload, stats, code=None, outcome=None):
    verdict = 'UNCERTAIN' if code else rollup(checks)
    model = {'name': stats.get('model', 'none'), 'version': stats.get('version', 'unknown'),
             'provider': stats.get('provider','google') if stats.get('calls') else None, 'calls': stats.get('calls', 0),
             'prompt_version': stage + '-facts-v1'}
    rec = build_record(request, agent_id=stage+'-manager@2', record_id=rid,
        captured_at=binding['captured_at'], client_id=binding.get('client_id'),
        unit_scope='order' if stage == 'pack' else 'unit', refs=binding.get('refs'), inputs=inputs,
        checks=checks, model=model, verdict=verdict, status='pending' if code else 'completed',
        outcome='pending_review' if code else outcome,
        reason=code or 'Deterministic assessment of registered sources and validated observations',
        needs_human=bool(code) or any(c['verdict']=='UNCERTAIN' for c in checks),
        error=error_obj(code, code, retryable=code in ('provider_timeout','provider_unavailable'), stage=stage) if code else None,
        payload={**payload, 'request_fingerprint': fingerprint, 'provider_attempts': stats.get('attempts', []),
                 'source_kind': 'trusted_registration', 'request_id': request['request_id']})
    return build_output(rec)


def make_app(stage, handle):
    app = FastAPI(title=stage.title()+' Manager')
    @app.get('/health')
    def health():
        try:
            configuration(stage)
            status = 'ok'
        except Exception:
            status = 'degraded'
        return {'status': status, 'stage': stage, 'version': '2', 'contract_version': '1.0'}
    @app.post('/run')
    async def run_http(request: Request):
        try:
            body = strict_json(await request.body())
        except (ValueError, UnicodeError):
            return JSONResponse({'error': 'invalid_json'}, status_code=422)
        try:
            return await run_in_threadpool(handle, body)
        except Rejected as exc:
            return JSONResponse({'error': exc.code}, status_code=exc.status)
        except Exception:
            return JSONResponse({'error': stage+'_unavailable'}, status_code=503)
    return app
