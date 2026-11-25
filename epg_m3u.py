"""Apply explicit EPG IDs only to unique dynamic M3U channels."""
import json
import re
from collections import Counter
from pathlib import Path


def apply_mappings(text, mappings):
    lines = text.splitlines(keepends=True)
    targets = {(row['group'], row['name']): row['epg_id'] for row in mappings}
    if len(targets) != len(mappings):
        raise ValueError('duplicate EPG mapping key')
    if any(not re.fullmatch(r'[A-Za-z0-9_.-]+', value) for value in targets.values()):
        raise ValueError('unsafe EPG ID')
    counts = Counter()
    def key_for(line):
        if not line.startswith('#EXTINF:') or ',' not in line:
            return None
        group = re.search(r'\bgroup-title="([^"]*)"', line)
        return (group.group(1), line.split(',', 1)[1].strip()) if group else None
    for line in lines:
        key = key_for(line)
        if key:
            counts[key] += 1
    applied, skipped = [], []
    for index, line in enumerate(lines):
        key = key_for(line)
        if key not in targets:
            continue
        if counts[key] != 1 or 'tvg-id=""' not in line:
            skipped.append(key)
            continue
        lines[index] = line.replace('tvg-id=""', f'tvg-id="{targets[key]}"', 1)
        applied.append(key)
    skipped.extend(key for key in targets if not counts[key])
    return ''.join(lines), {'applied': applied, 'skipped': skipped}


def load_mappings(path):
    config = json.loads(Path(path).read_text('utf-8'))
    if config['status'] != 'enabled':
        raise ValueError('EPG mapping must be explicitly enabled')
    return config['mappings']
