#!/usr/bin/env python3
"""Runtime-only priorities and canonical channel aliases for Daily index."""

from __future__ import annotations

import json
from pathlib import Path

CONFIG = Path("sources.json")

SMARTTV_URL = "http://dmi3y-tv6.ru/iptv/Playlist.m3u"

ALIASES = {
    # --- Required sport channels ---
    "khl": "KHL",
    "кхл": "KHL",
    "кхл (khl)": "KHL",

    "khl prime": "KHL PRIME",
    "кхл prime": "KHL PRIME",
    "кхл прайм": "KHL PRIME",
    "кхл (khl) prime": "KHL PRIME",

    "матч": "Матч ТВ",
    "матч!": "Матч ТВ",
    "матч тв": "Матч ТВ",
    "match tv": "Матч ТВ",

    "матч футбол 1": "Матч! Футбол 1",
    "матч! футбол 1": "Матч! Футбол 1",
    "match football 1": "Матч! Футбол 1",

    "матч футбол 2": "Матч! Футбол 2",
    "матч! футбол 2": "Матч! Футбол 2",
    "match football 2": "Матч! Футбол 2",

    "матч футбол 3": "Матч! Футбол 3",
    "матч! футбол 3": "Матч! Футбол 3",
    "match football 3": "Матч! Футбол 3",

    "волейбол": "Волейбол",
    "воллейбол": "Волейбол",
    "volleyball": "Волейбол",
    "voleybol": "Волейбол",
    "волейбол mobile": "Волейбол",

    "старт": "Старт",
    "start": "Старт",

    "setanta sport": "Setanta Sport",
    "setanta sports": "Setanta Sport",
    "setanta sport 1": "Setanta Sport",
    "setanta sports 1": "Setanta Sport",

    # --- Required radio channels ---
    "новое": "НОВОЕ",
    "новое радио": "НОВОЕ",
    "novoe": "НОВОЕ",
    "novoe radio": "НОВОЕ",

    "радио рекорд": "Record радио",
    "radiorecord": "Record радио",
    "record radio": "Record радио",
    "radio record": "Record радио",
    "record радио": "Record радио",
    "радио record": "Record радио",
}


def main() -> int:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))

    sources = cfg.get("sources", [])
    smarttv_found = False

    for source in sources:
        if not isinstance(source, dict):
            continue

        if source.get("url") == SMARTTV_URL:
            source["priority"] = 1
            source["trusted_russian"] = True
            smarttv_found = True

    if not smarttv_found:
        raise SystemExit(
            "Required source not found in sources.json: "
            + SMARTTV_URL
        )

    aliases = cfg.setdefault("russian_aliases", {})
    aliases.update(ALIASES)

    CONFIG.write_text(
        json.dumps(
            cfg,
            ensure_ascii=False,
            indent=2
        ) + "\n",
        encoding="utf-8"
    )

    print("Runtime overrides applied:")
    print("  dmi3y-tv SmartTV priority = 1")
    print(f"  canonical aliases added/updated = {len(ALIASES)}")
    print("  sport: KHL, KHL PRIME, Матч ТВ, Матч! Футбол 1/2/3, Волейбол, Старт, Setanta Sport")
    print("  radio: НОВОЕ, Record радио")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
