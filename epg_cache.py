"""Build a bounded, playlist-specific XMLTV cache without replacing a good guide on failure.

Usage: python epg_cache.py --config epg_sources.json --playlist playlistRU.m3u \
       --output /var/www/epg/guide.xml.gz --status /var/lib/gwz-epg/status.json
Run from a systemd timer. Serve the output as application/xml with Content-Encoding: gzip,
or as application/gzip at a .gz URL supported by the IPTV client.
"""

import argparse
import io
import gzip
import json
import os
import re
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path


def playlist_ids(path):
    text = Path(path).read_text('utf-8-sig')
    return set(re.findall(r'^#EXTINF:[^\n]*\btvg-id="([^"]+)"', text, re.M)) - {''}


def date(value):
    match = re.match(r'^(\d{12}(?:\d{2})?)(?:\s*([+-]\d{4}))?', value or '')
    if not match:
        raise ValueError('invalid XMLTV date')
    digits, offset = match.groups()
    if len(digits) == 12:
        digits += '00'
    tz = timezone.utc
    if offset:
        sign = 1 if offset[0] == '+' else -1
        minutes = int(offset[1:3]) * 60 + int(offset[3:])
        tz = timezone(sign * timedelta(minutes=minutes))
    return datetime.strptime(digits, '%Y%m%d%H%M%S').replace(tzinfo=tz).astimezone(timezone.utc)


def download(url, limit):
    if not url.startswith('https://'):
        raise ValueError('EPG source must use HTTPS')
    request = urllib.request.Request(url, headers={'User-Agent': 'GWZ-EPG/1.0'})
    with urllib.request.urlopen(request, timeout=35) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('compressed source too large')
    if data.startswith(b'\x1f\x8b'):
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as archive:
            data = archive.read(limit * 8 + 1)
    if len(data) > limit * 8:
        raise ValueError('uncompressed source too large')
    if b'<!DOCTYPE' in data[:4096].upper() or b'<!ENTITY' in data[:4096].upper():
        raise ValueError('XML external entities/DTD forbidden')
    return data


def build(config, ids, now=None, fetch=download):
    now = now or datetime.now(timezone.utc)
    lower, upper = now - timedelta(days=1), now + timedelta(days=5)
    channels, programmes, status = {}, {}, []
    for source in config['sources']:
        row = {'name': source['name'], 'status': 'error', 'programmes': 0}
        try:
            raw = fetch(source['url'], config.get('max_source_bytes', 32 * 1024 * 1024))
            root = ET.fromstring(raw)
            if root.tag != 'tv':
                raise ValueError('not XMLTV')
            source_channels = {e.get('id'): e for e in root.findall('channel') if e.get('id') in ids}
            added = 0
            source_programmes = {}
            for item in root.findall('programme'):
                cid = item.get('channel')
                if cid not in source_channels or cid in channels:
                    continue
                try:
                    start, stop = date(item.get('start')), date(item.get('stop'))
                except ValueError:
                    continue
                if not (start < stop and start < upper and stop > lower and item.find('title') is not None):
                    continue
                key = (cid, item.get('start'), item.get('stop'))
                source_programmes.setdefault(key, item)
            for key, item in source_programmes.items():
                programmes[key] = item
                channels.setdefault(key[0], source_channels[key[0]])
                added += 1
            row.update(status='ok', programmes=added)
        except (OSError, ValueError, ET.ParseError, EOFError) as exc:
            row['error'] = type(exc).__name__
        status.append(row)
    if not programmes:
        raise ValueError('no usable programmes; previous guide retained')
    tv = ET.Element('tv', {'generator-info-name': 'GWZ EPG cache'})
    for cid in sorted(channels):
        tv.append(channels[cid])
    for key in sorted(programmes, key=lambda k: (k[0], k[1], k[2])):
        tv.append(programmes[key])
    return ET.tostring(tv, encoding='utf-8', xml_declaration=True), {
        'updated_at': now.isoformat(), 'playlist_ids': len(ids),
        'covered_ids': len(channels), 'programmes': len(programmes), 'sources': status,
    }


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.epg-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--playlist', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--status', required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text('utf-8'))
    data, report = build(config, playlist_ids(args.playlist))
    if report['covered_ids'] < config.get('min_covered_ids', 1):
        raise ValueError('coverage below threshold; previous guide retained')
    old_status = Path(args.status)
    if old_status.exists():
        previous = json.loads(old_status.read_text('utf-8'))
        minimum = int(previous.get('covered_ids', 0) * 0.7)
        if report['covered_ids'] < minimum:
            raise ValueError('coverage dropped by over 30%; previous guide retained')
    atomic(args.output, gzip.compress(data, compresslevel=6, mtime=0))
    atomic(args.status, (json.dumps(report, ensure_ascii=False, indent=2) + '\n').encode())
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
