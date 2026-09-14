#!/usr/bin/env python3
"""Daily anonymous public-stream index. Python standard library + FFmpeg."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import hashlib
from html import unescape
from html.parser import HTMLParser
import ipaddress
import json
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit, urljoin, unquote
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
from urllib.error import HTTPError

RUSSIAN = {'ru', 'rus', 'russian', 'русский', 'рус'}

ATTR = re.compile(
    r'([\w-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')'
)

SECRET = re.compile(
    r'(?:^|[/;=_.-])'
    r'(?:token|auth|authorization|signature|sig|secret|password|passwd|'
    r'session|jwt|hmac|hdnea|hdnts|key|apikey|access_token)'
    r'(?:$|[/;=_.-])',
    re.I
)

OPAQUE = re.compile(
    r'[a-f0-9]{24,}|'
    r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}|'
    r'eyJ[A-Za-z0-9_.-]+',
    re.I
)


class Reject(Exception):
    """Short reason code only: never put third-party credentials into logs."""


class Budget:
    def __init__(self, byte_limit, seconds):
        self.limit = byte_limit
        self.deadline = time.monotonic() + seconds
        self.bytes = 0
        self.exhausted = False
        self.lock = threading.Lock()

    def account(self, count=0):
        with self.lock:
            self.bytes += count

            if self.bytes > self.limit or time.monotonic() > self.deadline:
                self.exhausted = True

            if self.exhausted:
                raise Reject('run_budget_exhausted')


def url_policy(url: str, strict: bool = True) -> None:
    decoded = url

    for _ in range(3):
        decoded = unquote(decoded)

    if re.search(r'[\s\\|\x00-\x1f]', decoded):
        raise Reject('url_syntax')

    try:
        p = urlsplit(decoded)
        port = p.port
    except ValueError:
        raise Reject('url_syntax')

    if p.scheme not in ('http', 'https') or not p.hostname:
        raise Reject('unsupported_scheme')

    if p.username or p.password or p.fragment:
        raise Reject('credentials_or_fragment')

    if strict and (p.query or '?' in decoded):
        raise Reject('query_parameters')

    if strict and (SECRET.search(p.path) or OPAQUE.search(p.path)):
        raise Reject('credential_like_path')

    if strict and re.search(
        r'/live/[^/]+/[^/]+/\d+(?:\.|/|$)',
        p.path,
        re.I
    ):
        raise Reject('subscription_path')

    if strict:
        for part in p.path.split('/'):
            if (
                len(part) >= 32
                and re.fullmatch(r'[A-Za-z0-9_-]+', part)
                and re.search(r'[A-Za-z]', part)
                and re.search(r'\d', part)
            ):
                raise Reject('opaque_path')

    host = p.hostname.casefold()

    if host == 'localhost' or host.endswith(
        ('.local', '.localhost', '.internal')
    ):
        raise Reject('nonpublic_address')

    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return

    if not addr.is_global:
        raise Reject('nonpublic_address')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Fetcher:
    def __init__(self, timeout=12, budget=None):
        self.timeout = timeout
        self.budget = budget

    def get(
        self,
        url,
        strict=True,
        limit=8 * 1024 * 1024,
        sample=False
    ):
        # No cookies, authentication, custom Referer,
        # source-supplied headers, or automatic redirects.
        # Validate each hop before requesting it.

        deadline = time.monotonic() + self.timeout * 2

        for _ in range(6):
            if self.budget:
                self.budget.account()

            url_policy(url, strict)

            p = urlsplit(url)

            try:
                addresses = socket.getaddrinfo(
                    p.hostname,
                    p.port or (443 if p.scheme == 'https' else 80),
                    type=socket.SOCK_STREAM
                )
            except OSError:
                raise Reject('dns_failed')

            if (
                not addresses
                or any(
                    not ipaddress.ip_address(a[4][0]).is_global
                    for a in addresses
                )
            ):
                raise Reject('nonpublic_address')

            # A fresh opener has neither cookie storage
            # nor credential handlers.
            opener = build_opener(
                ProxyHandler({}),
                NoRedirect()
            )

            try:
                response = opener.open(
                    Request(
                        url,
                        headers={
                            'User-Agent': 'daily-index/1.0',
                            'Accept-Encoding': 'identity'
                        }
                    ),
                    timeout=self.timeout
                )

            except HTTPError as e:
                if (
                    e.code in (301, 302, 303, 307, 308)
                    and e.headers.get('Location')
                ):
                    url = urljoin(url, e.headers['Location'])
                    e.close()
                    continue

                e.close()
                raise Reject('http_' + str(e.code))

            except OSError:
                raise Reject('network_failed')

            with response:
                if response.status != 200:
                    raise Reject('http_status')

                chunks = []
                size = 0

                while size <= limit:
                    if time.monotonic() > deadline:
                        raise Reject('download_timeout')

                    chunk = response.read(
                        min(65536, limit + 1 - size)
                    )

                    if not chunk:
                        break

                    chunks.append(chunk)
                    size += len(chunk)

                    if self.budget:
                        self.budget.account(len(chunk))

                    if sample and size >= limit:
                        break

                if size > limit and not sample:
                    raise Reject('download_limit')

                return b''.join(chunks)[:limit], url

        raise Reject('redirect_limit')


def decode(data):
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return data.decode('cp1251')


def norm(name):
    name = name.casefold().replace('ё', 'е')

    name = re.sub(
        r'\b(?:hd|sd|fhd|uhd|4k|1080p|720p|480p|360p)\b',
        '',
        name
    )

    # Keep time shifts (+2) and regional variants distinct.
    return ' '.join(re.findall(r'[\w+]+', name))


@dataclass
class Entry:
    name: str
    url: str
    group: str = 'Разное'
    tvg_id: str = ''
    language: str = ''
    source: str = ''
    restricted: bool = False


def parse_m3u(text, base='', source=''):
    current = None

    for line in text.splitlines():
        line = line.strip()

        if line.startswith('#EXTINF:'):
            # The separator comma may occur inside a quoted attribute.
            parts = re.split(
                r',(?=(?:[^"\']|"[^"]*"|\'[^\']*\')*$)',
                line,
                maxsplit=1
            )

            attrs = {
                m[0].lower(): m[1] or m[2]
                for m in ATTR.findall(parts[0])
            }

            name = (
                parts[1].strip()
                if len(parts) > 1
                else attrs.get('tvg-name', '')
            )

            current = Entry(
                name,
                '',
                attrs.get('group-title', 'Разное'),
                attrs.get('tvg-id', ''),
                attrs.get(
                    'tvg-language',
                    attrs.get('language', '')
                ),
                source
            )

        elif current and line.startswith(
            ('#EXTVLCOPT:', '#KODIPROP:', '#EXTHTTP:')
        ):
            current.restricted = True

        elif current and line and not line.startswith('#'):
            current.url = urljoin(base, line)
            yield current
            current = None


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in ('href', 'src') and value:
                self.urls.append(value)


def discover(text, base):
    parser = Links()
    parser.feed(text)

    urls = parser.urls + re.findall(
        r'https?://[^\s<>"\']+',
        unescape(text)
    )

    return sorted({
        urljoin(base, u)
        for u in urls
        if re.search(r'\.m3u8?(?:$|\?)', u, re.I)
    })


class LanguageIndex:
    def __init__(self, aliases, reference=()):
        self.names = {
            norm(k): v
            for k, v in aliases.items()
        }

        self.ids = {}

        for e in reference:
            self.names.setdefault(
                norm(e.name),
                e.name
            )

            if e.tvg_id:
                self.ids[e.tvg_id.casefold()] = e.name

    def identify(self, entry):
        langs = {
            x.casefold()
            for x in re.split(r'[,; /]+', entry.language)
            if x
        }

        # Если язык явно указан и он не русский — исключаем.
        if langs and not langs & RUSSIAN:
            return None

        # 1. Наш словарь известных названий.
        canonical = self.names.get(
            norm(entry.name)
        )

        if canonical:
            return norm(canonical), canonical

        # 2. Совпадение по tvg-id с русскоязычным эталоном.
        if entry.tvg_id.casefold() in self.ids:
            # Preserve shifts when names distinguish regional broadcasts.
            name = (
                entry.name
                if re.search(r'\+\d', entry.name)
                else self.ids[entry.tvg_id.casefold()]
            )

            return norm(name), name

        # 3. Канал явно помечен как русский.
        if langs & RUSSIAN:
            return norm(entry.name), entry.name

        # 4. Мягкий fallback для известных русскоязычных источников.
        #
        # Многие M3U из этих источников не содержат tvg-language,
        # поэтому раньше они массово отбрасывались.
        source = (entry.source or '').casefold()

        trusted_russian_source = (
            source.startswith('[dmi3y-tv]')
            or source.startswith('[loganet]')
            or 'твое тв' in source
            or 'твоё тв' in source
        )

        # Автоматически принимаем только название с кириллицей.
        # Это не даёт fallback-правилу добавить CNN, BBC и т.п.
        has_cyrillic_name = bool(
            re.search(
                r'[а-яё]',
                entry.name or '',
                re.IGNORECASE
            )
        )

        if trusted_russian_source and has_cyrillic_name:
            name = re.sub(
                r'\s+',
                ' ',
                entry.name
            ).strip()

            key = norm(name)

            if key:
                return key, name

        return None


def hls_plan(text, base):
    lines = [
        l.strip()
        for l in text.splitlines()
        if l.strip()
    ]

    if not lines or lines[0] != '#EXTM3U':
        raise Reject('not_hls')

    uris = []
    variants = []
    segments = []
    init = None

    pending = False
    bandwidth = {}
    current_bandwidth = 0

    for line in lines:
        if line.startswith(
            ('#EXT-X-KEY:', '#EXT-X-SESSION-KEY:')
        ):
            if not re.search(
                r'(?:^|[:,])METHOD=NONE(?:,|$)',
                line
            ):
                raise Reject('encrypted_hls')

        if line.startswith(
            (
                '#EXT-X-BYTERANGE',
                '#EXT-X-PART:',
                '#EXT-X-PRELOAD-HINT:',
                '#EXT-X-DEFINE:'
            )
        ):
            raise Reject('unsupported_hls_feature')

        embedded = re.findall(
            r'(?:^|[, :])URI="([^"]+)"',
            line
        )

        for uri in embedded:
            full = urljoin(base, uri)
            url_policy(full)
            uris.append(full)

        if (
            line.startswith('#EXT-X-MEDIA:')
            and embedded
        ):
            # Separate audio requires a matching audio rendition;
            # fail closed instead of publishing a video-only variant.
            if (
                'TYPE=AUDIO' in line
                or 'TYPE=VIDEO' in line
            ):
                raise Reject('external_rendition')

        if line.startswith('#EXT-X-MAP:'):
            if (
                'BYTERANGE=' in line
                or len(embedded) != 1
                or init
            ):
                raise Reject('unsupported_init')

            init = urljoin(base, embedded[0])

        if line.startswith('#EXT-X-STREAM-INF:'):
            pending = True

            match = re.search(
                r'(?:^|[:,])BANDWIDTH=(\d+)',
                line
            )

            current_bandwidth = (
                int(match[1])
                if match
                else 0
            )

        elif not line.startswith('#'):
            full = urljoin(base, line)
            url_policy(full)

            if pending:
                variants.append(full)
                bandwidth[full] = current_bandwidth
                pending = False
            else:
                segments.append(full)

    if not variants and (
        not segments
        or '#EXT-X-TARGETDURATION:' not in text
    ):
        raise Reject('not_media_playlist')

    return (
        sorted(
            variants,
            key=lambda u: bandwidth.get(u, 0)
        ),
        segments,
        init
    )


def probe_bytes(data, directory):
    media = Path(directory) / 'sample.bin'
    media.write_bytes(data)

    base = [
        '-v',
        'error',
        '-protocol_whitelist',
        'file,pipe',
        '-threads',
        '1'
    ]

    try:
        probe = subprocess.run(
            [
                'ffprobe',
                *base,
                '-show_streams',
                '-of',
                'json',
                str(media)
            ],
            capture_output=True,
            timeout=15
        )

        streams = json.loads(
            probe.stdout
        ).get('streams', [])

        types = {
            s.get('codec_type')
            for s in streams
        }

        if (
            probe.returncode
            or not {'video', 'audio'} <= types
        ):
            raise Reject('missing_audio_video')

        result = subprocess.run(
            [
                'ffmpeg',
                '-nostdin',
                *base,
                '-i',
                str(media),
                '-t',
                '2',
                '-map',
                '0:v:0',
                '-map',
                '0:a:0',
                '-vf',
                'scale=160:-2',
                '-f',
                'null',
                '-',
                '-progress',
                'pipe:1'
            ],
            capture_output=True,
            timeout=15
        )

        frames = [
            int(n)
            for n in re.findall(
                rb'frame=(\d+)',
                result.stdout
            )
        ]

        if (
            result.returncode
            or max(frames, default=0) < 10
        ):
            raise Reject('decode_failed')

        langs = {
            s.get(
                'tags',
                {}
            ).get(
                'language',
                ''
            ).lower()
            for s in streams
            if s.get('codec_type') == 'audio'
        } - {'', 'und'}

        if langs and not langs & RUSSIAN:
            raise Reject('audio_not_russian')

    except (
        subprocess.TimeoutExpired,
        json.JSONDecodeError
    ):
        raise Reject('probe_failed')


def check_stream(url, fetcher, depth=0):
    if depth > 3:
        raise Reject('hls_depth')

    data, final = fetcher.get(
        url,
        sample=True,
        limit=2 * 1024 * 1024
    )

    if data.lstrip(
        b'\xef\xbb\xbf \r\n'
    ).startswith(b'#EXTM3U'):

        # Fetch manifests fully so truncation
        # cannot conceal a key or token.
        data, final = fetcher.get(
            url,
            limit=2 * 1024 * 1024
        )

        variants, segments, init = hls_plan(
            decode(data),
            final
        )

        if variants:
            reasons = []

            for variant in variants:
                try:
                    return check_stream(
                        variant,
                        fetcher,
                        depth + 1
                    )
                except Reject as exc:
                    reasons.append(str(exc))

            raise Reject('no_working_variant')

        sample_parts = []

        if init:
            sample_parts.append(
                fetcher.get(
                    init,
                    limit=2 * 1024 * 1024
                )[0]
            )

        # Test several complete current media segments,
        # not only the manifest.
        for segment in segments[-2:]:
            sample_parts.append(
                fetcher.get(
                    segment,
                    limit=4 * 1024 * 1024
                )[0]
            )

        data = b''.join(sample_parts)

    else:
        # Only transport streams are accepted
        # as direct continuous binary feeds.
        #
        # HTML, MPD, playlists, MP4 downloads and scripts
        # do not pass this gate.
        if not any(
            data[i:i + 1] == b'G'
            and data[i + 188:i + 189] == b'G'
            and data[i + 376:i + 377] == b'G'
            for i in range(
                min(188, len(data))
            )
        ):
            raise Reject('not_direct_ts_or_hls')

    with tempfile.TemporaryDirectory(
        prefix='daily-index-'
    ) as temp:
        probe_bytes(data, temp)

    return final


def clean(value):
    return re.sub(
        r'[\x00-\x1f"<>]',
        '',
        str(value)
    ).strip()


def sorting_rules(path, aliases):
    groups = []
    lookup = {}
    group = 'Разное'

    if (
        not path
        or not Path(path).is_file()
    ):
        return groups, lookup

    for line in Path(path).read_text(
        'utf-8'
    ).splitlines():

        line = line.strip()

        if (
            line.startswith('[')
            and line.endswith(']')
        ):
            group = line[1:-1]
            groups.append(group)

        elif line:
            key = norm(line)
            key = norm(
                aliases.get(key, key)
            )

            lookup.setdefault(
                key,
                (
                    group,
                    len(lookup)
                )
            )

    return groups, lookup


def natural(value):
    return [
        (0, int(p))
        if p.isdigit()
        else (1, p)
        for p in re.split(
            r'(\d+)',
            value.casefold()
        )
    ]


def render(
    entries,
    order_file='',
    aliases=None
):
    out = ['#EXTM3U']
    seen = set()

    groups, lookup = sorting_rules(
        order_file,
        aliases or {}
    )

    # Source group spelling cannot fragment
    # the output into duplicate sections.
    group_aliases = {
        'федеральные': 'Федеральные',
        'новости': 'Новости',
        'news': 'Новости',
        'кино': 'Кино',
        'movies': 'Кино',
        'детские': 'Детские',
        'kids': 'Детские',
        'спорт': 'Спорт',
        'sport': 'Спорт',
        'sports': 'Спорт',
        'музыка': 'Музыка',
        'music': 'Музыка',
        'познавательные': 'Познавательные',
        'развлекательные': 'Развлекательные',
        '18+': '18+',
        '4k uhd': '4K / UHD'
    }

    for key, entry in entries.items():
        if key in lookup:
            entry.group = lookup[key][0]

        elif norm(entry.group) in group_aliases:
            entry.group = group_aliases[
                norm(entry.group)
            ]

        elif entry.group not in groups:
            entry.group = 'Разное'

    groups = [
        g
        for g in groups
        if g != '18+'
    ] + [
        'Разное',
        '18+'
    ]

    def sort_key(item):
        key, e = item

        return (
            groups.index(e.group)
            if e.group in groups
            else len(groups),

            lookup.get(
                key,
                ('', 100000)
            )[1],

            natural(e.name)
        )

    for key, e in sorted(
        entries.items(),
        key=sort_key
    ):
        url_policy(e.url)

        if e.url in seen:
            continue

        seen.add(e.url)

        out.extend([
            (
                f'#EXTINF:-1 '
                f'tvg-id="{clean(e.tvg_id)}" '
                f'tvg-language="Russian" '
                f'group-title="{clean(e.group)}",'
                f'{clean(e.name)}'
            ),
            e.url
        ])

    return '\n'.join(out) + '\n'


def write_atomic(path, content):
    path = Path(path)

    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    tmp = path.with_suffix(
        path.suffix + '.tmp'
    )

    tmp.write_text(
        content,
        encoding='utf-8'
    )

    tmp.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser()

    parser.add_argument(
        '--config',
        default='sources.json'
    )

    parser.add_argument(
        '--collect-only',
        action='store_true',
        help=(
            'diagnose sources without publishing '
            'an unchecked playlist'
        )
    )

    args = parser.parse_args(argv)

    cfg = json.loads(
        Path(
            args.config
        ).read_text('utf-8')
    )

    budget = Budget(
        cfg.get(
            'max_download_mib',
            1024
        ) * 1024 * 1024,

        cfg.get(
            'max_run_seconds',
            2100
        )
    )

    fetcher = Fetcher(
        cfg.get(
            'timeout',
            8
        ),
        budget
    )

    report = {
        'checked_at': datetime.now(
            timezone.utc
        ).isoformat(),
        'status': 'running',
        'sources': [],
        'rejected': {},
        'working_channels': 0
    }

    rejected = Counter()

    source_urls = {
        s['url']: s['name']
        for s in cfg['sources']
    }

    workers = cfg.get(
        'workers',
        8
    )

    def read_source(item):
        url, name = item

        try:
            raw, final = fetcher.get(
                url,
                strict=False,
                limit=cfg.get(
                    'max_source_bytes',
                    8388608
                )
            )

            content = decode(raw)

            entries = (
                list(
                    parse_m3u(
                        content,
                        final,
                        name
                    )
                )
                if content.lstrip().startswith(
                    '#EXTM3U'
                )
                else []
            )

            return entries, {
                'name': name,
                'status': (
                    'ok'
                    if entries
                    else 'not_playlist'
                ),
                'entries': len(entries)
            }

        except (Reject, OSError) as exc:
            return [], {
                'name': name,
                'status': (
                    str(exc)
                    if isinstance(exc, Reject)
                    else 'network_failed'
                ),
                'entries': 0
            }

    for page in cfg.get(
        'discovery_pages',
        []
    ):
        try:
            raw, final = fetcher.get(
                page,
                strict=False
            )

            found = discover(
                decode(raw),
                final
            )

            if len(found) > cfg.get(
                'max_discovered_playlists',
                300
            ):
                raise Reject(
                    'discovery_limit'
                )

            for url in found:
                source_urls.setdefault(
                    url,
                    (
                        urlsplit(page).hostname
                        + ' / discovered'
                    )
                )

            report['sources'].append({
                'name': urlsplit(page).hostname,
                'status': 'discovered',
                'playlists': len(found)
            })

        except (Reject, OSError):
            report['sources'].append({
                'name': urlsplit(page).hostname,
                'status': 'discovery_failed'
            })

    reference, reference_status = read_source(
        (
            cfg['language_reference'],
            'Russian-language metadata reference'
        )
    )

    report[
        'language_reference'
    ] = reference_status

    language = LanguageIndex(
        cfg['russian_aliases'],
        reference
    )

    entries = []

    with ThreadPoolExecutor(
        max_workers=workers
    ) as executor:

        for result, status in executor.map(
            read_source,
            source_urls.items()
        ):
            entries.extend(result)
            report['sources'].append(
                status
            )

    report[
        'downloaded_entries'
    ] = len(entries)

    previous = {}

    state_path = Path(
        cfg['state']
    )

    if state_path.exists():
        previous = json.loads(
            state_path.read_text(
                'utf-8'
            )
        ).get(
            'channels',
            {}
        )

        entries.extend(
            Entry(**e)
            for e in previous.values()
        )

    candidates = defaultdict(dict)

    for e in entries:
        try:
            url_policy(e.url)

            if e.restricted:
                raise Reject(
                    'custom_headers_or_keys'
                )

            match = language.identify(e)

            if not match:
                raise Reject(
                    'language_unknown_or_non_russian'
                )

            key, canonical = match

            e.name = canonical

            candidates[key].setdefault(
                e.url,
                e
            )

        except Reject as exc:
            rejected[str(exc)] += 1

    report[
        'candidate_channels'
    ] = len(candidates)

    report[
        'candidate_urls'
    ] = sum(
        len(v)
        for v in candidates.values()
    )

    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    'downloaded_entries',
                    'candidate_channels',
                    'candidate_urls'
                )
            },
            ensure_ascii=False
        ),
        flush=True
    )

    if args.collect_only:
        report.update(
            status='collection_only',
            rejected=dict(rejected)
        )

        write_atomic(
            cfg['report'],
            json.dumps(
                report,
                ensure_ascii=False,
                indent=2
            )
        )

        return 0

    checked_urls = {}
    checked_lock = threading.Lock()

    def check_once(url):
        with checked_lock:
            owner = (
                url not in checked_urls
            )

            if owner:
                checked_urls[
                    url
                ] = Future()

            future = checked_urls[url]

        if owner:
            try:
                for attempt in range(2):
                    try:
                        future.set_result(
                            check_stream(
                                url,
                                fetcher
                            )
                        )

                        break

                    except Reject as exc:
                        if (
                            attempt == 0
                            and str(exc) in {
                                'network_failed',
                                'download_timeout',
                                'http_503',
                                'probe_failed'
                            }
                        ):
                            continue

                        raise

            except Exception as exc:
                future.set_exception(exc)

        return future.result()

    def choose(item):
        key, urls = item

        prior_url = previous.get(
            key,
            {}
        ).get('url')

        ordered = sorted(
            urls.values(),
            key=lambda e: (
                e.url != prior_url,
                not e.url.startswith(
                    'https:'
                ),
                e.url
            )
        )

        errors = Counter()

        for entry in ordered:
            try:
                entry.url = check_once(
                    entry.url
                )

                return (
                    key,
                    entry,
                    errors
                )

            except Reject as exc:
                errors[
                    str(exc)
                ] += 1

            except OSError:
                errors[
                    'network_or_probe_error'
                ] += 1

        return (
            key,
            None,
            errors
        )

    selected = {}
    missing = []

    with ThreadPoolExecutor(
        max_workers=workers
    ) as executor:

        for key, entry, errors in executor.map(
            choose,
            candidates.items()
        ):
            rejected.update(errors)

            if entry:
                selected[key] = entry
            else:
                missing.append(key)

    try:
        budget.account()

    except Reject:
        pass

    if budget.exhausted:
        report.update(
            status='failed_run_budget',
            downloaded_bytes=budget.bytes,
            rejected=dict(rejected)
        )

        write_atomic(
            cfg['report'],
            json.dumps(
                report,
                ensure_ascii=False,
                indent=2
            )
        )

        print(
            'Run limit reached; '
            'incomplete result was not published.'
        )

        return 1

    text = render(
        selected,
        cfg.get(
            'order_file',
            ''
        ),
        cfg['russian_aliases']
    )

    report[
        'downloaded_bytes'
    ] = budget.bytes

    report.update(
        status=(
            'ok'
            if selected
            else 'failed_no_verified_streams'
        ),

        rejected=dict(rejected),

        working_channels=text.count(
            '#EXTINF:'
        ),

        unavailable_channels=missing,

        replaced_channels=[
            k
            for k, e in selected.items()
            if (
                k in previous
                and e.url != previous[k]['url']
            )
        ],

        removed_channels=sorted(
            set(previous)
            - set(selected)
        )
    )

    report[
        'playlist_sha256'
    ] = (
        hashlib.sha256(
            text.encode()
        ).hexdigest()
        if selected
        else None
    )

    if selected:
        write_atomic(
            cfg['output'],
            text
        )

        write_atomic(
            cfg['state'],
            json.dumps(
                {
                    'checked_at': report[
                        'checked_at'
                    ],
                    'channels': {
                        k: asdict(e)
                        for k, e in selected.items()
                    }
                },
                ensure_ascii=False,
                indent=2
            )
        )

    write_atomic(
        cfg['report'],
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2
        )
    )

    print(
        json.dumps(
            {
                'status': report['status'],
                'working_channels': report[
                    'working_channels'
                ]
            },
            ensure_ascii=False
        )
    )

    return (
        0
        if selected
        else 1
    )


if __name__ == '__main__':
    raise SystemExit(main())
