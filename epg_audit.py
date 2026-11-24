"""Read-only coverage report and conservative XMLTV metadata suggestions.

Run: python epg_audit.py playlistRU.m3u --guide guide.xml.gz --output epg_audit.json
The report never changes a playlist or claims a name match is verified.
"""

import argparse
import gzip
import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path


def records(text):
    for line in text.splitlines():
        if not line.startswith('#EXTINF:'):
            continue
        attr = lambda key: (re.search(r'\b' + key + r'="([^"]*)"', line) or [None, ''])[1]
        yield {'name': line.split(',', 1)[-1].strip(), 'group': attr('group-title'),
               'id': attr('tvg-id'), 'logo': attr('tvg-logo')}


def key(name):
    # Exact name after case and whitespace normalization ONLY. No HD/Prime,
    # time-shift, punctuation or transliteration rewriting: these can be
    # distinct broadcasts and must not silently inherit another EPG.
    return ' '.join(name.casefold().split())


def catalog(xml):
    root = ET.fromstring(xml)
    if root.tag != 'tv':
        raise ValueError('not XMLTV')
    by_name = defaultdict(set)
    icons = {}
    for channel in root.findall('channel'):
        cid = channel.get('id', '')
        if not cid:
            continue
        for title in channel.findall('display-name'):
            if title.text:
                by_name[key(title.text)].add(cid)
        icon = channel.find('icon')
        if icon is not None and icon.get('src', '').startswith('https://'):
            icons[cid] = icon.get('src')
    return by_name, icons


def audit(text, xml=None):
    entries = list(records(text))
    by_name, icons = catalog(xml) if xml is not None else ({}, {})
    by_group = defaultdict(lambda: {'channels': 0, 'ids': 0, 'logos': 0})
    suggestions = []
    for entry in entries:
        stats = by_group[entry['group']]
        stats['channels'] += 1
        stats['ids'] += bool(entry['id'])
        stats['logos'] += bool(entry['logo'])
        if not entry['id'] and len(by_name.get(key(entry['name']), ())) == 1:
            cid = next(iter(by_name[key(entry['name'])]))
            suggestions.append({'name': entry['name'], 'group': entry['group'],
                                'candidate_id': cid, 'candidate_logo': icons.get(cid, ''),
                                'status': 'review_required'})
    return {'channels': len(entries), 'ids': sum(bool(x['id']) for x in entries),
            'logos': sum(bool(x['logo']) for x in entries),
            'groups': dict(sorted(by_group.items())),
            'suggestions': suggestions}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('playlist')
    parser.add_argument('--guide')
    parser.add_argument('--output', default='epg_audit.json')
    args = parser.parse_args()
    guide = Path(args.guide).read_bytes() if args.guide else None
    if guide and guide.startswith(b'\x1f\x8b'):
        guide = gzip.decompress(guide)
    report = audit(Path(args.playlist).read_text('utf-8-sig'), guide)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    print(json.dumps({key: report[key] for key in ('channels', 'ids', 'logos')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
