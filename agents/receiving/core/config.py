import os
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]     # agents/receiving
REPO_ROOT = Path(__file__).resolve().parents[3]     # pod repo root

class Config:
    gemini_api_key: str = os.environ.get("RECEIVING_GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
    gemini_model: str = os.environ.get("RECEIVING_GEMINI_MODEL") or os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview")
    gemini_fallback_models: list = [m.strip() for m in os.environ.get(
        "GEMINI_FALLBACK_MODELS", "").split(",") if m.strip()]
    cache_dir: Path = Path(os.environ.get("RECEIVING_CACHE_DIR", REPO_ROOT / "out" / "receiving-cache"))
    timeout_s: float = float(os.environ.get("RECEIVING_TIMEOUT_S", "20"))
    prompts_dir: Path = AGENT_DIR / "prompts"

CFG = Config()

# Input capture root: refs resolve ONLY inside this directory (see app._safe_rel)
DATA_INPUT = Path(os.environ.get("INPUT_DIR", str(REPO_ROOT / "data" / "input")))
