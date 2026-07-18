from __future__ import annotations

"""Shared meme catalog access (read-only manifest) used by the site + pool API."""

import json
import random

from x402_pow.config import MEMES_DIR

_manifest_cache: dict | None = None


def _load_raw() -> list[dict]:
    manifest = MEMES_DIR / "manifest.json"
    if not manifest.exists():
        return []
    try:
        return json.loads(manifest.read_text()).get("memes", [])
    except Exception:
        return []


def load_manifest(*, refresh: bool = False) -> list[dict]:
    global _manifest_cache
    if _manifest_cache is None or refresh:
        memes = _load_raw()
        _manifest_cache = {"list": memes, "index": {m["id"]: m for m in memes if m.get("id")}}
    return _manifest_cache["list"]


def meme_index() -> dict[str, dict]:
    load_manifest()
    assert _manifest_cache is not None
    return _manifest_cache["index"]


def meme_by_id(meme_id: str | None) -> dict | None:
    if not meme_id:
        return None
    return meme_index().get(meme_id)


def pick_random() -> dict | None:
    memes = load_manifest()
    if not memes:
        return None
    return random.choice(memes)
