"""Explicit provider selection and secret-safe Round 3 diagnostics; no decision policy."""
from dataclasses import asdict, replace
import json
import re
import threading

from .core.returns_manager.domain import ValidationError
from .core.returns_manager.gemini import GeminiConfig, GeminiVisionProvider
from .core.returns_manager.groq import GroqConfig, GroqVisionProvider, GroqHTTPFailure, MODEL
from .core.returns_manager.ollama import OllamaConfig, OllamaVisionProvider
from .core.returns_manager.observations import ProviderResponse
from .core.returns_manager.vision import ProviderUnavailable
from .request_store import Rejected


GEMINI_ERRORS = frozenset({
    "gemini_authentication_failed", "gemini_rate_limited", "gemini_http_error",
    "gemini_network_error", "gemini_redirect_rejected", "gemini_request_rejected",
    "gemini_model_unavailable", "gemini_overloaded", "gemini_unavailable",
})
GROQ_STATUS = {400: "groq_request_rejected", 401: "groq_authentication_failed",
               403: "groq_authentication_failed", 404: "groq_model_unavailable",
               429: "groq_rate_limited", 503: "groq_overloaded"}


class CloudBoundary:
    """Keep the original engine error vocabulary; expose only fixed diagnostics.

    Thread-local diagnostics cannot cross concurrent requests. Raw exception text
    is never retained. Credential/header-bearing responses are rejected whole.
    """
    mode = "real"

    def __init__(self, provider, key):
        self.provider = provider
        self.name = provider.name
        self._key = key
        self._local = threading.local()

    @property
    def diagnostic(self):
        return getattr(self._local, "diagnostic", None)

    def observe(self, request):
        self._local.diagnostic = None
        try:
            response = self.provider.observe(request)
        except TimeoutError:
            self._local.diagnostic = self.name + "_timeout"
            raise TimeoutError("provider_timeout") from None
        except GroqHTTPFailure as exc:
            self._local.diagnostic = GROQ_STATUS.get(exc.status, "groq_http_error")
            raise ProviderUnavailable("provider_unavailable") from None
        except ProviderUnavailable as exc:
            self._local.diagnostic = (str(exc) if self.name == "gemini" and str(exc) in GEMINI_ERRORS
                                      else self.name + "_unavailable")
            raise ProviderUnavailable("provider_unavailable") from None
        except (ValidationError, ValueError, TypeError, KeyError, AttributeError, IndexError):
            self._local.diagnostic = self.name + "_invalid_response"
            # Let the unchanged engine reject a non-response as invalid_response.
            return None
        except Exception:
            self._local.diagnostic = self.name + "_failure"
            raise RuntimeError("provider_failure") from None
        if isinstance(response, ProviderResponse):
            values = asdict(response)
            try:
                values["decoded_text"] = json.loads(response.text)
            except (ValueError, TypeError, RecursionError):
                pass  # Canonical parser remains responsible for malformed JSON.
            if self._contains_secret(values):
                self._local.diagnostic = self.name + "_unsafe_response"
                return None
        return response

    def _contains_secret(self, value):
        if isinstance(value, str):
            return self._key in value or bool(re.search(r"authorization|x-goog-api-key|bearer\s+", value, re.I))
        if isinstance(value, dict):
            return any(self._contains_secret(k) or self._contains_secret(v) for k, v in value.items())
        if isinstance(value, (list, tuple)):
            return any(self._contains_secret(v) for v in value)
        return False

    def diagnose_validation_failure(self, capture, images, response):
        diagnostic = getattr(self.provider, "diagnose_validation_failure", None)
        if callable(diagnostic):
            diagnostic(capture, images, response)


def validate_selection(selection):
    """Reject unknown selectors and secret-bearing settings even for stored replay."""
    if not selection:
        raise Rejected("explicit_real_provider_required")
    if type(selection) is not dict or set(selection) - {"name", "settings"}:
        raise Rejected("invalid_provider_configuration")
    name = selection.get("name")
    if type(name) is not str or name not in {"ollama", "gemini", "groq"}:
        raise Rejected("unknown_returns_provider")
    settings = selection.get("settings", {})
    allowed = {"ollama": {"base_url", "model", "timeout_seconds"},
               "gemini": {"model", "timeout_seconds", "free_tier_confirmed"},
               "groq": {"timeout_seconds"}}[name]
    if type(settings) is not dict or set(settings) - allowed:
        raise Rejected("invalid_provider_configuration")
    return name, settings


def select_provider(selection, *, provider=None):
    """Return provider and credential-free effective replay configuration.

    Keys are environment-only; unknown settings are rejected before persistence.
    An injected provider is an offline test seam, never an automatic fallback.
    """
    name, settings = validate_selection(selection)
    try:
        if name == "ollama":
            config = OllamaConfig(**settings)
            factory = OllamaVisionProvider
        else:
            config_type, factory = ((GeminiConfig, GeminiVisionProvider) if name == "gemini"
                                    else (GroqConfig, GroqVisionProvider))
            config = replace(config_type.from_env(), **settings)
            if not config.api_key:
                raise Rejected(name + "_key_missing")
            if name == "gemini" and not config.free_tier_confirmed:
                raise Rejected("gemini_free_tier_confirmation_required")
        if config.timeout_seconds > 28:
            raise Rejected("provider_timeout_exceeds_adapter_budget")
    except Rejected:
        raise
    except Exception:
        raise Rejected(name + "_invalid_configuration") from None
    try:
        selected = provider if provider is not None else factory(config)
        if selected.mode != "real":
            raise Rejected("live_provider_must_be_real")
        if name != "ollama" and selected.name != name:
            raise Rejected("provider_selection_mismatch")
    except Rejected:
        raise
    except Exception:
        raise Rejected(name + "_initialization_failed", 503) from None
    if name == "ollama":
        return selected, selection  # Preserve existing Ollama/replay behavior.
    public = {"timeout_seconds": config.timeout_seconds,
              "model": config.model if name == "gemini" else MODEL}
    if name == "gemini":
        public["free_tier_confirmed"] = config.free_tier_confirmed
    return CloudBoundary(selected, config.api_key), {"name": name, "settings": public}
