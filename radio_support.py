#!/usr/bin/env python3
"""Bounded, audio-only radio verification. Video rules remain unchanged."""
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
import json
import re
from urllib.parse import urlsplit
from pathlib import Path
import subprocess
import tempfile

def audio_bytes(url, fetcher, depth=0):
    """Fetch a small sample with the main builder's redirect, DNS and URL policy."""
    from refresh import Reject, decode, hls_plan
    if depth > 2:
        raise Reject('radio_hls_depth')
    data, final = fetcher.get(url, sample=True, limit=131072)
    if data.lstrip(b'\xef\xbb\xbf \r\n').startswith(b'#EXTM3U'):
        variants, segments, init = hls_plan(decode(data), final)
        if variants:
            for variant in variants[:2]:
                try:
                    return audio_bytes(variant, fetcher, depth + 1)
                except Reject:
                    continue
            raise Reject('radio_no_working_variant')
        if init:
            raise Reject('radio_unsupported_init')
        if not segments:
            raise Reject('radio_no_segments')
        data = fetcher.get(segments[-1], sample=True, limit=131072)[0]
    return data

def check_audio_sample(data):
    """Require an actual decoded audio stream; do not require video or imagery."""
    from refresh import Reject
    if len(data) < 2048:
        raise Reject('radio_sample_short')
    with tempfile.TemporaryDirectory(prefix='daily-radio-') as directory:
        media = Path(directory) / 'sample.bin'
        media.write_bytes(data)
        try:
            probe = subprocess.run(
                ['ffprobe', '-v', 'error', '-probesize', '131072',
                 '-analyzeduration', '2000000', '-show_streams',
                 '-of', 'json', str(media)],
                capture_output=True, timeout=8
            )
            streams = json.loads(probe.stdout or b'{}').get('streams', [])
            if probe.returncode or not any(
                x.get('codec_type') == 'audio' for x in streams
            ):
                raise Reject('radio_missing_audio')
            decoded = subprocess.run(
                ['ffmpeg', '-nostdin', '-v', 'error', '-i', str(media),
                 '-map', '0:a:0', '-t', '0.5', '-f', 'null', '-'],
                capture_output=True, timeout=8
            )
            if decoded.returncode:
                raise Reject('radio_decode_failed')
        except (subprocess.TimeoutExpired, ValueError, json.JSONDecodeError):
            raise Reject('radio_probe_failed')
    return True

# Mediaplano / Mediascope, Mar 2025–May 2026, 33 named stations.
# New Radio and Record are pinned as requested in the app.
TOP_SPB = [["Новое Радио","new radio"],["Radio Record","Record Dance","Радио Рекорд","Record","Радио Рекорд (основной канал)"],["Дорожное радио","Dorozhnoe Radio","Dorognoe Radio"],["Европа Плюс","Europa Plus"],["Ретро FM","Retro FM"],["Эльдорадио","Eldoradio"],["Авторадио","Avtoradio"],["Русское Радио","Russian Radio"],["Вести FM","Vesti FM"],["Питер FM","Piter FM"],["Business FM","Бизнес FM"],["Радио Ваня","Ваня"],["Радио Шансон","Шансон Радио","Radio Shanson","Шансон"],["Monte Carlo","Радио Монте Карло","Радио Монте-Карло","Radio Monte Carlo"],["Радио Дача","Radio Dacha"],["Наше Радио","Nashe Radio"],["Радио Эрмитаж","Radio Hermitage","Эрмитаж"],["Maximum","Радио Максимум","Radio Maximum"],["Радио России","Radio Russia","Radio Rossii"],["Маяк","Радио Маяк","Radio Mayak","Mayak"],["Comedy Radio"],["Юмор FM","Humor FM"],["Love Radio"],["Хит FM","Hit FM"],["DFM","Ди ФМ"],["Комсомольская правда","Радио Комсомольская правда","Radio Komsomolskaya Pravda"],["Детское Радио","Detskoe Radio"],["Relax FM"],["Радио Звезда","Звезда","Radio Zvezda"],["Radio Metro","Радио Метро"],["STUDIO 21","Студио 21"],["Like FM"],["Royal Radio"]]

def station_key(name):
    name = name.casefold().replace('ё', 'е').replace('fm', 'фм')
    return ''.join(c for c in name if c.isalnum())

def top_name(name):
    variants = {station_key(name), station_key(re.split(r' \(| - | \|', name, maxsplit=1)[0])}
    for aliases in TOP_SPB:
        if any(station_key(a) in variants for a in aliases):
            return aliases[0]
    return None

def record_host(url):
    host = (urlsplit(url).hostname or '').casefold()
    return host == 'radiorecord.ru' or host.endswith('.radiorecord.ru') or host == 'radiorecord.hostingradio.ru' or bool(re.fullmatch(r'hls-[0-9]+-radiorecord\.hostingradio\.ru', host))

def expanded_candidates(candidates, preferred, norm):
    """Spread the extra catalog across genres instead of taking only its first group."""
    first = []
    buckets = defaultdict(deque)
    for entry in candidates:
        if norm(entry.name) in preferred:
            first.append(entry)
        else:
            buckets[entry.group.casefold()].append(entry)
    while buckets:
        for group in list(buckets):
            first.append(buckets[group].popleft())
            if not buckets[group]:
                del buckets[group]
    return first


def collect_radio(source_url, order_file, max_stations=30, max_probes=55,
                  workers=6, mode='general', max_seconds=None, existing_keys=()):
    """Use the Russian-country radio directory; verify bounded audio candidates."""
    from refresh import Budget, Fetcher, Reject, parse_m3u, norm, url_policy
    stats = Counter()
    if mode == 'expanded':
        source_fetcher = Fetcher(timeout=5, budget=Budget(96 * 1024 * 1024, min(240, int(max_seconds or 240))))
    else:
        source_fetcher = Fetcher(timeout=6, budget=Budget(40 * 1024 * 1024, 220)) if mode != 'general' else Fetcher(timeout=6, budget=Budget(12 * 1024 * 1024, 130))
    try:
        raw, final = source_fetcher.get(source_url, strict=False, limit=(4 if mode == 'expanded' else 1) * 1024 * 1024)
        candidates = list(parse_m3u(raw.decode('utf-8-sig'), final,
                                    source='[radio] AlonDrilich Russia'))
    except (Reject, UnicodeError, OSError):
        return [], {'status': 'source_failed', 'checked': 0, 'working': 0}

    preferred = set()
    try:
        lines = Path(order_file).read_text('utf-8').splitlines()
        in_radio = False
        for line in lines:
            line = line.strip()
            if line.startswith('[') and line.endswith(']'):
                in_radio = 'Радио' in line
            elif in_radio and line:
                preferred.add(norm(line))
    except OSError:
        pass

    unique = []
    existing_keys = set(existing_keys)
    seen = set()
    per_station = Counter()
    for entry in candidates:
        try:
            url_policy(entry.url)
            if entry.unsafe_options or not entry.name.strip() or entry.url in seen:
                continue
        except Reject:
            continue
        if mode == 'expanded':
            group = entry.group.casefold()
            if ('info' in group or group.strip() in {'record', 'инфо'}
                    or record_host(entry.url) or top_name(entry.name)
                    or station_key(entry.name) in existing_keys):
                continue
        if mode == 'record':
            if not record_host(entry.url) or entry.group.strip().casefold() != 'record':
                continue
            if top_name(entry.name) == 'Radio Record':
                entry.name = 'Radio Record'
            else:
                entry.name = 'Record ' + re.sub(r'^record\s+', '', entry.name.strip(), flags=re.I)
        elif mode == 'top':
            name = top_name(entry.name)
            if not name:
                continue
            entry.name = name
        if mode != 'general':
            key = station_key(entry.name)
            if per_station[key] >= 3:
                continue
            per_station[key] += 1
        seen.add(entry.url)
        unique.append(entry)

    if mode == 'top':
        rank = {station_key(a[0]): i for i, a in enumerate(TOP_SPB)}
        unique.sort(key=lambda x: rank[station_key(x.name)])
    elif mode == 'record':
        unique.sort(key=lambda x: x.name != 'Radio Record')
    elif mode == 'expanded':
        unique = expanded_candidates(unique, preferred, norm)
    else:
        unique.sort(key=lambda x: (norm(x.name) not in preferred, candidates.index(x)))
    chosen = unique[:max(1, int(max_probes))]
    def check(entry):
        try:
            data = audio_bytes(entry.url, source_fetcher)
            check_audio_sample(data)
            entry.group = '📻 Радио Record' if mode == 'record' else '📻 Радио'
            entry.language = 'Russian'  # Existing publication contract; radio audio language is not verified.
            entry.source = '[radio] ' + mode
            return entry, 'ok'
        except (Reject, OSError):
            return None, 'failed'

    result = []
    accepted_names = set()
    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 8))) as pool:
        for entry, status in pool.map(check, chosen):
            stats[status] += 1
            if entry and (mode == 'general' or station_key(entry.name) not in accepted_names):
                accepted_names.add(station_key(entry.name))
                result.append(entry)
            if len(result) >= max_stations:
                break
    return result, {
        'status': 'ok' if result else 'no_verified_audio',
        'catalog_entries': len(candidates),
        'checked': stats['ok'] + stats['failed'],
        'working': len(result)
    }
