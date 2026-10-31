#!/usr/bin/env python3
"""Verify the fixed Sports Complex before including it in the daily main IPTV list.

The source fixed_prefix.m3u is immutable. This generates a runtime-only
filtered prefix; it never writes unchecked sports URLs into the published M3U.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from refresh import Budget, Fetcher, Reject, check_stream, parse_m3u, url_policy

SPORT_GROUP = 'group-title="🏆 Спорткомплекс"'


def split_records(text: str):
    """Keep all source bytes/line endings and every non-sport record intact."""
    header, records, active = [], [], None
    for line in text.splitlines(keepends=True):
        if line.startswith('#EXTINF:'):
            if active is not None:
                records.append(''.join(active))
            active = [line]
        elif active is None:
            header.append(line)
        else:
            active.append(line)
    if active is not None:
        records.append(''.join(active))
    return ''.join(header), records


def is_sport(record: str) -> bool:
    return SPORT_GROUP in record.splitlines()[0]


def entry_for(record: str):
    entries = list(parse_m3u(record))
    if len(entries) != 1:
        raise Reject('not_single_stream')
    entry = entries[0]
    if entry.unsafe_options:
        raise Reject('unsupported_custom_options')
    url_policy(entry.url)
    return entry


def verify_stream(entry, budget, timeout=6):
    fetcher = Fetcher(timeout=timeout, budget=budget)
    try:
        check_stream(
            entry.url, fetcher,
            user_agent=entry.user_agent, referrer=entry.referrer
        )
        return 'LIVE'
    except Reject as exc:
        return str(exc)
    except (OSError, ValueError, RuntimeError):
        return 'probe_error'


def verify_records(text, probe, *, workers=4):
    """Probe is injectable, so offline tests exercise selection without network."""
    header, records = split_records(text)
    results = {}
    originals = []
    pending = {}
    seen = set()
    reasons = Counter()
    sport_total = 0
    duplicates = 0

    for idx, record in enumerate(records):
        if not is_sport(record):
            continue
        sport_total += 1
        try:
            entry = entry_for(record)
        except (Reject, ValueError) as exc:
            results[idx] = False
            reasons[str(exc)] += 1
            continue

        identity = (entry.url, entry.user_agent, entry.referrer)
        if identity in seen:
            results[idx] = False
            duplicates += 1
            reasons['duplicate_stream'] += 1
            continue
        seen.add(identity)
        originals.append((idx, entry))

    if not sport_total:
        raise RuntimeError('No original fixed Sports Complex streams found')

    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 8))) as pool:
        pending = {pool.submit(probe, entry): idx for idx, entry in originals}
        for future in as_completed(pending):
            idx = pending[future]
            try:
                result = future.result()
            except (OSError, ValueError, RuntimeError, Reject):
                result = 'probe_error'
            ok = result == 'LIVE'
            results[idx] = ok
            if not ok:
                reasons[str(result)[:80]] += 1

    kept = sum(results.values())
    output = header + ''.join(
        record for idx, record in enumerate(records)
        if not is_sport(record) or results.get(idx, False)
    )
    report = {
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'original_sports': sport_total,
        'probed_unique_sports': len(originals),
        'verified_sports': kept,
        'excluded_sports': sport_total - kept,
        'duplicates': duplicates,
        'reasons': dict(sorted(reasons.items())),
        'original_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'verified_prefix_sha256': hashlib.sha256(output.encode('utf-8')).hexdigest(),
    }
    return output, report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', default='fixed_prefix.m3u')
    parser.add_argument('--output', default='fixed_prefix_checked.m3u')
    parser.add_argument('--report', default='sports_status.json')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--seconds', type=int, default=420)
    args = parser.parse_args(argv)

    original = Path(args.source).read_text(encoding='utf-8-sig')
    budget = Budget(256 * 1024 * 1024, args.seconds)
    def probe(entry):
        return verify_stream(entry, budget)

    filtered, report = verify_records(original, probe, workers=args.workers)
    previous_path = Path(args.report)
    previous = {}
    if previous_path.exists():
        try:
            previous = json.loads(previous_path.read_text('utf-8'))
        except (OSError, ValueError):
            previous = {}

    prior_good = int(previous.get('verified_sports', 0) or 0)
    current_good = report['verified_sports']
    drastic_drop = prior_good >= 6 and current_good < max(1, prior_good // 3)

    if budget.exhausted:
        report['status'] = 'failed_probe_budget'
    elif current_good == 0:
        report['status'] = 'failed_no_sports_verified'
    elif drastic_drop:
        report['status'] = 'failed_mass_sport_outage'
    else:
        report['status'] = 'ok'

    # Logs and published report contain only aggregate statistics, never stream
    # URLs or user-provided HTTP header values.
    if report['status'] == 'ok':
        dest = Path(args.output)
        tmp = dest.with_name(dest.name + '.tmp')
        tmp.write_text(filtered, encoding='utf-8')
        tmp.replace(dest)

    temp = previous_path.with_name(previous_path.name + '.tmp')
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(previous_path)
    print(json.dumps({k: report[k] for k in (
        'status', 'original_sports', 'probed_unique_sports',
        'verified_sports', 'excluded_sports', 'reasons'
    )}, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
