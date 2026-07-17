#!/usr/bin/env python3
"""Download unique full-size Phneep posters into apps/demo_site/static/memes/."""

from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
MEMES = ROOT / "apps" / "demo_site" / "static/memes"
PROP_URL = "https://phneep.com/propaganda/"

PREFER = [
    "Honey-Badger",
    "Not-Your-Keys",
    "Proof-of-Work",
    "Run-a-Node",
    "Hodl",
    "Hoooodl",
    "Behodl",
    "NYET",
    "Shitcoin",
    "Enlist",
    "Bitcoin-Fixes",
    "Stack-Sats",
    "Diamond",
    "Moon",
    "Chancellor",
    "Cold-Storage",
    "Sleeves",
    "Dont-Trust",
    "Full-Node",
]


def is_thumb(url: str) -> bool:
    return bool(re.search(r"-\d{2,4}x\d{2,4}\.(jpg|jpeg|png|webp)$", url, re.I))


def title_from_url(url: str) -> str:
    name = Path(unquote(url.split("?")[0])).stem
    name = re.sub(r"-\d{2,4}x\d{2,4}$", "", name)
    name = re.sub(r"[-_]+", " ", name)
    name = re.sub(r"\bweb\b", "", name, flags=re.I).strip()
    return name or "Bitcoin propaganda"


def main(limit: int = 30) -> None:
    MEMES.mkdir(parents=True, exist_ok=True)
    # clear old phneep_* only
    for p in MEMES.glob("phneep_*"):
        p.unlink()

    html = urllib.request.urlopen(PROP_URL, timeout=60).read().decode("utf-8", "replace")
    urls = set(re.findall(r'https?://[^"\s>]+\.(?:jpg|jpeg|png|webp)', html, re.I))
    for r in re.findall(r'(/wp-content/uploads/[^"\s>]+\.(?:jpg|jpeg|png|webp))', html, re.I):
        urls.add("https://phneep.com" + r)

    full = [u for u in urls if not is_thumb(u)]
    # dedupe by basename without size
    by_base: dict[str, str] = {}
    for u in full:
        base = re.sub(r"-\d{2,4}x\d{2,4}(?=\.(jpg|jpeg|png|webp)$)", "", Path(u).name, flags=re.I)
        # prefer longer / non-scaled
        if base not in by_base or len(u) > len(by_base[base]):
            # Prefer URL without dimension suffix
            if not is_thumb(u):
                by_base[base] = u

    candidates = list(by_base.values())

    def score(u: str) -> int:
        s = 0
        for i, kw in enumerate(PREFER):
            if kw.lower() in u.lower():
                s += 200 - i
        return s

    ranked = sorted(candidates, key=score, reverse=True)[:limit]

    memes = []
    for i, url in enumerate(ranked):
        ext = Path(url.split("?")[0]).suffix.lower() or ".jpg"
        fname = f"phneep_{i:02d}{ext}"
        dest = MEMES / fname
        try:
            print(f"fetch {title_from_url(url)}")
            req = urllib.request.Request(url, headers={"User-Agent": "x402-pow-meme-bot/0.1"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                dest.write_bytes(resp.read())
            time.sleep(0.25)
            memes.append(
                {
                    "id": f"phneep-{i:02d}",
                    "file": fname,
                    "title": title_from_url(url),
                    "credit": "Phneep — phneep.com/propaganda",
                    "sourceUrl": url,
                    "tags": ["phneep", "propaganda"],
                }
            )
        except Exception as e:
            print(f"skip: {e}")

    if (MEMES / "magic_internet_money.png").exists():
        memes.insert(
            0,
            {
                "id": "mim-wizard",
                "file": "magic_internet_money.png",
                "title": "Magic Internet Money",
                "credit": "u/mavensbot / r/Bitcoin 2013",
                "sourceUrl": "https://knowyourmeme.com/memes/magic-internet-money-bitcoin-wizard",
                "tags": ["mim", "wizard"],
            },
        )

    (MEMES / "manifest.json").write_text(
        json.dumps({"memes": memes, "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, indent=2)
    )
    print(f"Wrote {len(memes)} unique memes → {MEMES}")


if __name__ == "__main__":
    main()
