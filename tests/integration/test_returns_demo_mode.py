"""Returns demo mode must start without a cloud key; every other mode must still fail closed."""
import json
from pathlib import Path

import pytest

from agents.returns.adapter import Adapter
from agents.returns.request_store import Rejected

ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "data" / "input"


def _config():
    return json.loads((INPUT / "returns_live_config.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _no_ambient_provider_env(monkeypatch):
    for name in ("GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_FREE_TIER_CONFIRMED", "RETURNS_DEMO_MODE"):
        monkeypatch.delenv(name, raising=False)


def test_live_binding_without_key_fails_closed(tmp_path):
    with pytest.raises(Rejected) as raised:
        Adapter(_config(), INPUT, tmp_path / "state")
    assert raised.value.code == "gemini_key_missing"


def test_demo_mode_starts_without_a_key_and_has_no_cloud_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("RETURNS_DEMO_MODE", "1")
    adapter = Adapter(_config(), INPUT, tmp_path / "state")
    assert adapter.provider is None, "demo mode must not hold a cloud provider it will never call"


def test_demo_flag_does_not_swallow_other_configuration_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("RETURNS_DEMO_MODE", "1")
    config = _config()
    config["provider"] = {"name": "gemini", "settings": {"model": "not-a-free-model", "free_tier_confirmed": True}}
    with pytest.raises(Exception):
        Adapter(config, INPUT, tmp_path / "state")
