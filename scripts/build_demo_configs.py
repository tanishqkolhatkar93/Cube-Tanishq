import hashlib
import io
import json
import os
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared.utils import sample_data
from agents.prep.core.rulepacks.organizer import RULE_VERSION

root = Path("data/input")
configs = {s: {'version': 1, 'bindings': []} for s in ('prep', 'pack', 'recovery')}
cases = json.loads(Path("data/sample/cases.json").read_text())
gemini_model = os.environ.get('GEMINI_MODEL', 'gemini-3-flash-preview')


def key_env(stage):
    specific = stage.upper() + '_GEMINI_API_KEY'
    return specific if os.environ.get(specific) else 'GEMINI_API_KEY'


def prep_image(unit):
    image = Image.new("RGB", (1000, 650), "white")
    draw = ImageDraw.Draw(image)
    try:
        title_font = ImageFont.truetype("arial.ttf", 48)
        body_font = ImageFont.truetype("arial.ttf", 34)
    except OSError:
        title_font = ImageFont.load_default()
        body_font = ImageFont.load_default()
    draw.rectangle((60, 60, 940, 590), outline="black", width=4)
    draw.text((90, 95), "PREP INSPECTION DEMO CAPTURE", fill="black", font=title_font)
    draw.rectangle((110, 210, 890, 330), outline="black", width=3)
    draw.text((145, 245), "FNSKU X001ABC123", fill="black", font=title_font)
    draw.text((145, 360), "Flat label on front face", fill="black", font=body_font)
    draw.rectangle((145, 440, 520, 510), fill="black")
    draw.text((550, 456), "Original barcode covered", fill="black", font=body_font)
    draw.text((90, 540), f"Unit {unit}", fill="black", font=body_font)
    stream = io.BytesIO()
    image.save(stream, "PNG")
    return stream.getvalue()


def write_item(ref, raw):
    path = root / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return {'ref': ref, 'path': ref, 'sha256': hashlib.sha256(raw).hexdigest()}


for case in cases:
    unit, org = case['unit_id'], case['org_id']
    for stage in ('prep', 'pack', 'recovery'):
        if stage == 'prep' and case['route'] != 'fba':
            continue
        if stage == 'pack' and case['route'] != 'mfn':
            continue
        binding = {'org_id': org, 'subject_id': unit, 'workflow_id': f'WF-{org}-{unit}',
                   'captured_at': '2026-10-01T10:00:00Z', 'refs': {}, 'files': [],
                   'trusted_agents': {'receiving': ['receiving-manager@1.0.0'],
                                      'prep': ['pod14-prep-manager@1'], 'pack': ['pack-manager@2'],
                                      'returns': ['returns-manager@1']}}
        if stage == 'recovery' and unit == 'UNIT-0014' and org == 'org_demo_alpha':
            binding['client_id'] = 'client_alpha'
        if stage == 'prep':
            criteria = {
                'criteria_id': 'demo-prep-' + unit,
                'version': '1',
                'source': 'Local demo criteria for Cube Round 3 operator validation',
                'rule_pack': RULE_VERSION,
                'sku': 'DEMO-' + unit,
                'expected_fnsku': 'X001ABC123',
                'requires_polybag': False,
                'requires_suffocation_warning': False,
                'cover_original_barcode': True,
                'has_expiry': False,
                'required_handling_marks': []
            }
            criteria_ref = f'{unit}/prep/criteria.json'
            image_ref = f'{unit}/prep/front.png'
            criteria_item = write_item(criteria_ref, json.dumps(criteria, sort_keys=True, separators=(',', ':')).encode())
            image_item = write_item(image_ref, prep_image(unit))
            configs[stage]['bindings'].append({
                'subject_id': unit,
                'workflow_id': f'WF-{org}-{unit}',
                'capture_id': 'DEMO-PREP-' + unit,
                'captured_at': '2026-10-01T10:00:00Z',
                'operator_id': 'local-demo-operator',
                'criteria': criteria_item,
                'images': [image_item],
            })
            continue
        if stage == 'pack':
            ref = f'{unit}/pack/synthetic.png'
            binding['refs'] = {'order_id': 'SYNTHETIC-' + unit}
            binding['order_lines'] = {'SYNTHETIC-' + unit: 1}
            stream = io.BytesIO()
            Image.new('RGB', (40, 40), (140, 120, 160)).save(stream, 'PNG')
            raw = stream.getvalue()
        else:
            ref = f'{unit}/recovery/report.json'
            lines = [{'line_id': r['line_id'], 'charge_type': r['charge_type'], 'amount_usd': r['amount_usd'],
                      'currency': 'USD', 'unit_scope': 'unit', 'refs': {}} for r in sample_data.fee_lines(unit, org)]
            raw = json.dumps({'org_id': org, 'subject_id': unit, 'workflow_id': binding['workflow_id'],
                              'complete': True, 'line_count': len(lines), 'lines': lines}).encode()
        item = {**write_item(ref, raw), 'kind': 'image' if stage == 'pack' else 'document'}
        binding['files'] = [item]
        configs[stage]['bindings'].append(binding)

configs['prep'].update({
    'org_id': 'org_demo_alpha',
    'client_id': None,
    'provider': {
        'kind': 'gemini',
        'model': gemini_model,
        'api_key_env': key_env('prep'),
        'deadline_s': 15,
    },
})

for stage in ('pack', 'recovery'):
    configs[stage]['provider'] = {
        'model': gemini_model,
        'api_key_env': key_env(stage),
        'deadline_s': 15,
    }

for stage in configs:
    (root / (stage + '_config.json')).write_text(json.dumps(configs[stage], indent=2))
    (root / stage).mkdir(exist_ok=True) # state dir
print("Configs generated in data/input/")

