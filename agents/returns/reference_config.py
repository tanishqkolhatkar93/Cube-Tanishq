"""Build the optional reference-comparison dependencies from the environment (opt-in, fail closed).

  RETURNS_REFERENCE_COMPARE=1        turn the feature on (anything else: off, behaviour unchanged)
  REFERENCE_BACKEND=dir|supabase     where the reference library lives (default dir)
  RETURNS_REFERENCE_DIR=data/reference           for dir (layout: <org>/<sku>/<angle>/<image>)
  SUPABASE_URL / SUPABASE_SERVICE_KEY            for supabase (server-side secrets, never in the browser)
  GEMINI_API_KEY, GEMINI_FREE_TIER_CONFIRMED=1   the vision model; without them every comparison is UNCERTAIN
"""
from __future__ import annotations

import os

from .reference_compare import GeminiReferenceComparer
from .reference_store import DirReferenceStore, SupabaseReferenceStore


def from_env():
    """-> (reference_store, reference_comparer), or (None, None) when the feature is off."""
    if os.environ.get("RETURNS_REFERENCE_COMPARE") != "1":
        return None, None
    backend = os.environ.get("REFERENCE_BACKEND", "dir").strip().lower()
    if backend == "dir":
        store = DirReferenceStore(os.environ.get("RETURNS_REFERENCE_DIR", "data/reference"))
    elif backend == "supabase":
        url, key = os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_SERVICE_KEY", "")
        if not url or not key:
            raise RuntimeError("REFERENCE_BACKEND=supabase requires SUPABASE_URL and SUPABASE_SERVICE_KEY")
        store = SupabaseReferenceStore(url, key)
    else:
        raise ValueError("REFERENCE_BACKEND must be 'dir' or 'supabase'")
    return store, GeminiReferenceComparer()
