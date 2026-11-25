"""Validate proposed mappings against the current read-only EPG probe report."""

import argparse
import json
from pathlib import Path


def validate(config, report):
    if config['source'] != report['source']:
        raise ValueError('EPG source differs from reviewed source')
    if config['status'] != 'review_only_not_published':
        raise ValueError('mapping review status changed')
    rows = report['name_review_entries']
    seen = set()
    for mapping in config['mappings']:
        key = (mapping['group'], mapping['name'])
        if key in seen:
            raise ValueError('duplicate mapping: ' + repr(key))
        seen.add(key)
        matches = [row for row in rows if (row['group'], row['name']) == key]
        if len(matches) != 1 or matches[0]['reason'] != 'unique_name_candidate':
            raise ValueError('mapping no longer unique: ' + repr(key))
        if matches[0]['candidate_ids'] != [mapping['epg_id']] or not matches[0]['has_programme']:
            raise ValueError('mapping ID or schedule changed: ' + repr(key))
    return {'verified_review_candidates': len(seen), 'published': False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text('utf-8'))
    report = json.loads(Path(args.report).read_text('utf-8'))
    print(json.dumps(validate(config, report)))


if __name__ == '__main__':
    main()
