#!/usr/bin/env python3
"""Bounded, audio-only radio verification. Video rules remain unchanged."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
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

def collect_radio(source_url, order_file, max_stations=30, max_probes=55,
                  workers=6):
    """Use the Russian-country radio directory; verify bounded audio candidates."""
    from refresh import Budget, Fetcher, Reject, parse_m3u, norm, url_policy
    stats = Counter()
    source_fetcher = Fetcher(timeout=6, budget=Budget(12 * 1024 * 1024, 130))
    try:
        raw, final = source_fetcher.get(source_url, strict=False, limit=1024 * 1024)
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
    seen = set()
    for entry in candidates:
        try:
            url_policy(entry.url)
            if entry.unsafe_options or not entry.name.strip() or entry.url in seen:
                continue
        except Reject:
            continue
        seen.add(entry.url)
        unique.append(entry)

    unique.sort(key=lambda x: (norm(x.name) not in preferred,
                               candidates.index(x)))
    chosen = unique[:max(1, int(max_probes))]
    def check(entry):
        try:
            data = audio_bytes(entry.url, source_fetcher)
            check_audio_sample(data)
            entry.group = '📻 Радио'
            entry.language = 'Russian'  # Playlist market; not a claim about audio speech.
            entry.source = '[radio] AlonDrilich Russia'
            return entry, 'ok'
        except (Reject, OSError):
            return None, 'failed'

    result = []
    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 8))) as pool:
        for entry, status in pool.map(check, chosen):
            stats[status] += 1
            if entry:
                result.append(entry)
            if len(result) >= max_stations:
                break
    return result, {
        'status': 'ok' if result else 'no_verified_audio',
        'catalog_entries': len(candidates),
        'checked': stats['ok'] + stats['failed'],
        'working': len(result)
    }
