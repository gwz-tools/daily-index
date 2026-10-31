#!/usr/bin/env python3
"""Build an optional sports-only M3U from existing sources; no third-party API keys."""
from __future__ import annotations

import argparse
from pathlib import Path
import re

from refresh import (parse_m3u, url_policy, Reject, clean,
                     public_channel_name, safe_header_value, safe_referrer)

SPORT_NAMES = re.compile(
    r'кхл|\bkhl\b|\bnhl\b|матч|футбол|football|soccer|setanta|'
    r'euro\s?sport|viasat\s+sport|\bстарт\b|\bспорт\b|\bsport\b|'
    r'бокс\s*тв|волейбол|баскетбол|хоккей|триумф|удар|arena', re.I
)
HOCKEY = re.compile(r'кхл|\bkhl\b|\bnhl\b|хоккей', re.I)
FOOTBALL = re.compile(r'матч|футбол|football|soccer|setanta|\bliga\b', re.I)


def topic(name: str) -> tuple[int, str]:
    if HOCKEY.search(name):
        return 0, '🏒 Хоккей / КХЛ'
    if FOOTBALL.search(name):
        return 1, '⚽ Футбол / Матч!'
    return 2, '🏆 Другой спорт'


def sports_entries(fixed: str, dynamic: str):
    """Prefer verified dynamic entries when identical URL+headers occur in fixed."""
    seen = set()
    rows = []
    for verified, text in ((True, dynamic), (False, fixed)):
        for entry in parse_m3u(text):
            if not SPORT_NAMES.search(entry.name) and 'спорт' not in entry.group.casefold():
                continue
            if entry.unsafe_options:
                continue
            try:
                url_policy(entry.url)
                user_agent = safe_header_value(entry.user_agent)
                referrer = safe_referrer(entry.referrer)
            except Reject:
                continue
            ident = (entry.url, user_agent, referrer)
            if ident in seen:
                continue
            seen.add(ident)
            rows.append((verified, entry))
    return sorted(rows, key=lambda item: (
        topic(item[1].name)[0], not item[0],
        item[1].name.casefold(), item[1].url,
    ))


def render_sports(rows) -> str:
    out = ['#EXTM3U']
    for verified, entry in rows:
        _, category = topic(entry.name)
        status = 'проверено' if verified else 'резерв без свежей проверки'
        group = clean(f'{category} · {status}')
        name = clean(public_channel_name(entry.name))
        attrs = (
            f'tvg-id="{clean(entry.tvg_id)}" '
            f'tvg-name="{name}" '
            f'tvg-language="Russian" '
            f'group-title="{group}"'
        )
        if entry.tvg_logo:
            attrs += f' tvg-logo="{clean(entry.tvg_logo)}"'
        out.append(f'#EXTINF:-1 {attrs},{name}')
        if entry.user_agent:
            out.append('#EXTVLCOPT:http-user-agent=' + safe_header_value(entry.user_agent))
        if entry.referrer:
            out.append('#EXTVLCOPT:http-referrer=' + safe_referrer(entry.referrer))
        out.append(entry.url)
    return '\n'.join(out) + '\n'


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--playlist', default='playlistRU.m3u')
    parser.add_argument('--fixed', default='fixed_prefix.m3u')
    parser.add_argument('--output', default='sportsRU.m3u')
    args = parser.parse_args()
    playlist = Path(args.playlist).read_text('utf-8-sig')
    fixed = Path(args.fixed).read_text('utf-8-sig').strip() + '\n'
    if not playlist.startswith(fixed):
        raise SystemExit('Fixed prefix mismatch; refusing to build sports playlist')
    dynamic = playlist[len(fixed):]
    rows = sports_entries(fixed, dynamic)
    if not rows:
        raise SystemExit('No sports channels; existing sports playlist preserved')
    result = render_sports(rows)
    tmp = Path(args.output).with_suffix('.tmp')
    tmp.write_text(result, encoding='utf-8')
    tmp.replace(args.output)
    print(f'Sports playlist: {len(rows)} entries, dynamic verified: {sum(x[0] for x in rows)}, fixed unverified: {sum(not x[0] for x in rows)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
