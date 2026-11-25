"""Validate proposed mappings against the current read-only EPG probe report."""

import argparse
import json
from pathlib import Path


def validate(config, report):
    if config['source'] != report['source']:
        raise ValueError('EPG source differs from reviewed source')
    if config['status'] not in ('review_only_not_published', 'enabled'):
        raise ValueError('mapping status invalid')
    rows = report['name_review_entries']
    seen = set()
    for mapping in config['mappings']:
        key = (mapping['group'], mapping['name'])
        if key in seen:
            raise ValueError('duplicate mapping: ' + repr(key))
        seen.add(key)
        matches = [row for row in rows if (row['group'], row['name']) == key]
        direct = [row for row in report.get('direct_mapping_entries', [])
                  if (row['group'], row['name']) == key]
        candidate_ok = (len(matches) == 1 and matches[0]['reason'] == 'unique_name_candidate'
                        and matches[0]['candidate_ids'] == [mapping['epg_id']]
                        and matches[0]['has_programme'])
        direct_ok = (len(direct) == 1 and direct[0]['id'] == mapping['epg_id']
                     and direct[0]['has_programme'])
        if not (candidate_ok or direct_ok):
            raise ValueError('mapping not uniquely verified: ' + repr(key))
    return {'verified_review_candidates': len(seen),
            'published': config['status'] == 'enabled'}


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
