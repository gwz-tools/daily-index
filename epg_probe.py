"""Read-only, bounded XMLTV probe for the current IPTV playlist.

Downloads one source, measures channel/EPG coverage and seven-day horizon.
Never changes the playlist, guide, or Dropbox.
"""

import argparse
import gzip
import json
import re
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


def read_playlist(path):
    entries = []
    for line in Path(path).read_text('utf-8-sig').splitlines():
        if not line.startswith('#EXTINF:'):
            continue
        def attr(name):
            match = re.search(r'\b' + name + r'="([^"]*)"', line)
            return match.group(1) if match else ''
        entries.append({'name': line.split(',', 1)[-1].strip(),
                        'id': attr('tvg-id'), 'group': attr('group-title'),
                        'logo': attr('tvg-logo')})
    return entries


def normalized(value):
    return ' '.join(value.casefold().split())


def tvtime(value):
    match = re.match(r'^(\d{12}(?:\d{2})?)(?:\s*([+-])(\d{2})(\d{2}))?', value or '')
    if not match:
        return None
    stamp, sign, hours, minutes = match.groups()
    try:
        time = datetime.strptime(stamp, '%Y%m%d%H%M%S' if len(stamp) == 14 else '%Y%m%d%H%M')
        offset = timedelta(hours=int(hours or 0), minutes=int(minutes or 0))
        return time.replace(tzinfo=timezone(offset if sign != '-' else -offset)).astimezone(timezone.utc)
    except ValueError:
        return None


class LimitedReader:
    def __init__(self, stream, limit):
        self.stream, self.limit, self.total = stream, limit, 0

    def read(self, size=-1):
        data = self.stream.read(size)
        self.total += len(data)
        if self.total > self.limit:
            raise ValueError('expanded XMLTV exceeds safety limit')
        return data


def download(url, path, max_bytes=32 * 1024 * 1024):
    if not url.startswith('https://'):
        raise ValueError('source must be HTTPS')
    request = urllib.request.Request(url, headers={'User-Agent': 'GWZ-EPG-Probe/1.0'})
    with urllib.request.urlopen(request, timeout=45) as response, open(path, 'wb') as output:
        size = 0
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise ValueError('compressed source exceeds safety limit')
            output.write(chunk)
    return size


def analyse(stream, entries, now=None):
    now = now or datetime.now(timezone.utc)
    ids = {entry['id'] for entry in entries if entry['id']}
    names = {normalized(entry['name']) for entry in entries if entry['name']}
    catalog, icons = defaultdict(set), set()
    programmes = Counter()
    future_days = defaultdict(set)
    root = None
    source_channels = 0
    for event, element in ET.iterparse(stream, events=('start', 'end')):
        if root is None:
            if element.tag != 'tv':
                raise ValueError('not XMLTV')
            root = element
        if event != 'end' or element is root:
            continue
        if element.tag == 'channel':
            source_channels += 1
            cid = element.get('id', '')
            if cid:
                for child in element.findall('display-name'):
                    if child.text and normalized(child.text) in names:
                        catalog[normalized(child.text)].add(cid)
                if element.find('icon') is not None:
                    icons.add(cid)
        elif element.tag == 'programme':
            cid = element.get('channel', '')
            if cid:
                start = tvtime(element.get('start'))
                stop = tvtime(element.get('stop'))
                if start and stop and now - timedelta(days=1) <= start < now + timedelta(days=8) and stop > now:
                    programmes[cid] += 1
                    for day in range(7):
                        begin = now + timedelta(days=day)
                        if start < begin + timedelta(days=1) and stop > begin:
                            future_days[day].add(cid)
        if element.tag in ('channel', 'programme'):
            root.clear()
    exact_names = {name: next(iter(cids)) for name, cids in catalog.items() if len(cids) == 1}
    exact = [e for e in entries if not e['id'] and normalized(e['name']) in exact_names]
    direct = [e for e in entries if e['id'] and programmes[e['id']]]
    return {
        'playlist_entries': len(entries), 'playlist_with_id': sum(bool(e['id']) for e in entries),
        'playlist_with_logo': sum(bool(e['logo']) for e in entries),
        'source_channels': source_channels,
        'direct_epg_entries': len(direct),
        'direct_epg_ids': len({e['id'] for e in direct}),
        'exact_name_candidates': len(exact),
        'exact_name_with_programmes': sum(bool(programmes[exact_names[normalized(e['name'])]]) for e in exact),
        'source_icons_for_direct_ids': len({e['id'] for e in direct} & icons),
        'future_days_direct_ids': [len({e['id'] for e in entries if e['id']} & future_days[day]) for day in range(7)],
        'candidates_for_review': sorted(({'name': e['name'], 'group': e['group'],
                                           'candidate_id': exact_names[normalized(e['name'])],
                                           'has_programme': bool(programmes[exact_names[normalized(e['name'])]])}
                                          for e in exact), key=lambda e: (e['group'], e['name'])),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--playlist', required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    entries = read_playlist(args.playlist)
    with tempfile.TemporaryDirectory() as directory:
        source_file = Path(directory) / 'source.xml.gz'
        compressed = download(args.source, source_file)
        with gzip.open(source_file, 'rb') as raw:
            stream = LimitedReader(raw, 512 * 1024 * 1024)
            report = analyse(stream, entries)
            report['expanded_bytes'] = stream.total
    report['compressed_bytes'] = compressed
    report['checked_at'] = datetime.now(timezone.utc).isoformat()
    report['source'] = args.source
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'candidates_for_review'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
