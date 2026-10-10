"""Lazy Google SDK boundary. Every attempted call is counted, with a deadline."""
import time
from .base import VisionProvider, VisionResponse
from ..config import CFG
from ..failures import Failure, classify

class GeminiProvider(VisionProvider):
    name = "gemini"

    def __init__(self):
        if not CFG.gemini_api_key:
            raise Failure("provider_configuration_required")
        # SDK absence does not prevent deterministic paths/imports.
        try:
            from google import genai
            from google.genai import types
            self.types = types
            self.client = genai.Client(api_key=CFG.gemini_api_key,
                http_options=types.HttpOptions(timeout=int(CFG.timeout_s * 1000),
                                               retry_options=types.HttpRetryOptions(attempts=1)))
        except Exception:
            raise Failure("provider_initialization_failed") from None
        self.models = [CFG.gemini_model, *CFG.gemini_fallback_models]

    def close(self):
        self.client.close()

    def analyze(self, req, *, stats, deadline):
        last = "model_error"
        for model in self.models:
            for _ in range(2):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Failure("agent_timeout")
                stats["calls"] += 1
                attempt = {"model": model, "outcome": "started"}
                stats["attempts"].append(attempt)
                started = time.monotonic()
                try:
                    resp = self.client.models.generate_content(model=model,
                        contents=[self.types.Part.from_bytes(data=req.image_bytes, mime_type="image/jpeg"), req.prompt_text],
                        config=self.types.GenerateContentConfig(
                            response_mime_type="application/json", response_schema=req.schema_model,
                            http_options=self.types.HttpOptions(timeout=max(1, int(remaining * 1000)),
                                retry_options=self.types.HttpRetryOptions(attempts=1))))
                    if time.monotonic() > deadline:
                        raise Failure("agent_timeout")
                    parsed_value = getattr(resp, "parsed", None)
                    if parsed_value is not None:
                        parsed = req.schema_model.model_validate(parsed_value)
                    else:
                        parsed = req.schema_model.model_validate_json(resp.text)
                    attempt["outcome"] = "success"
                    usage = getattr(resp, "usage_metadata", None)
                    return VisionResponse(parsed=parsed, raw_text=resp.text, model_id=model,
                        prompt_version=req.prompt_version, latency_ms=int((time.monotonic()-started)*1000),
                        tokens=int(getattr(usage, "total_token_count", 0) or 0))
                except Exception as exc:
                    last = classify(exc)
                    attempt["outcome"] = last
                    if last == "agent_timeout":
                        raise Failure(last) from None
        raise Failure(last)
