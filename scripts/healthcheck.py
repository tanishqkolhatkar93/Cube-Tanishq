"""Container health probe: exit 0 only if the orchestrator reports status ok."""
import json
import os
import sys
import urllib.request

url = f"http://127.0.0.1:{os.environ.get('PORT', '8100')}/health"
try:
    with urllib.request.urlopen(url, timeout=5) as response:
        sys.exit(0 if json.load(response).get("status") == "ok" else 1)
except Exception as exc:  # noqa: BLE001 - any failure means unhealthy
    print(f"unhealthy: {exc}", file=sys.stderr)
    sys.exit(1)
