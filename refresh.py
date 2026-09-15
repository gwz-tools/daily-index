#!/usr/bin/env python3
"""Daily public-stream index: fixed prefix + verified dynamic fallback channels."""
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

# Keep stream verification deliberately lightweight.  A full HLS segment can
# be several megabytes; probing thousands of candidates with full segments can
# exhaust the per-run download budget long before the scan finishes.
MANIFEST_SAMPLE_BYTES = 256 * 1024
DIRECT_SAMPLE_BYTES = 256 * 1024
INIT_SAMPLE_BYTES = 128 * 1024
SEGMENT_SAMPLE_BYTES = 256 * 1024

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

FIXED_GROUPS = {
    '📺 Общие каналы WINK 🇷🇺',
    '▶️ Rutube (VPN)',
    '🏆 Спорткомплекс',
    '📷 КАМЕРЫ (ONLINE)',
}


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
        _ = p.port
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


def safe_header_value(value: str) -> str:
    value = str(value or '').strip()
    if '\r' in value or '\n' in value or '\x00' in value:
        raise Reject('header_syntax')
    return value[:1024]


def safe_referrer(value: str) -> str:
    value = safe_header_value(value)
    if not value:
        return ''
    try:
        p = urlsplit(value)
    except ValueError:
        raise Reject('referrer_syntax')
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise Reject('referrer_syntax')
    return value


def request_headers(user_agent='', referrer=''):
    headers = {
        'User-Agent': safe_header_value(user_agent) or 'daily-index/1.0',
        'Accept-Encoding': 'identity',
    }
    if referrer:
        headers['Referer'] = safe_referrer(referrer)
    return headers


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
        sample=False,
        user_agent='',
        referrer=''
    ):
        # No cookies or authentication. Only User-Agent and Referer from an M3U
        # entry are supported. Redirect hops are validated before requesting them.
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

            opener = build_opener(
                ProxyHandler({}),
                NoRedirect()
            )

            try:
                response = opener.open(
                    Request(
                        url,
                        headers=request_headers(user_agent, referrer)
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
    # Некоторые IPTV/HLS-серверы возвращают gzip даже при
    # Accept-Encoding: identity.
    if data.startswith(b'\x1f\x8b'):
        import gzip

        try:
            data = gzip.decompress(data)
        except (OSError, EOFError):
            raise Reject('invalid_gzip')

    try:
        return data.decode('utf-8-sig')

    except UnicodeDecodeError:
        try:
            return data.decode('cp1251')

        except UnicodeDecodeError:
            raise Reject('text_decode_failed')


def norm(name):
    name = name.casefold().replace('ё', 'е')

    name = re.sub(
        r'\b(?:hd|sd|fhd|uhd|4k|1080p|720p|480p|360p)\b',
        '',
        name
    )

    # Keep time shifts (+2 etc.) and regional variants distinct.
    return ' '.join(re.findall(r'[\w+]+', name))


@dataclass
class Entry:
    name: str
    url: str
    group: str = 'Разное'
    tvg_id: str = ''
    tvg_logo: str = ''
    language: str = ''
    source: str = ''
    restricted: bool = False
    unsafe_options: bool = False
    user_agent: str = ''
    referrer: str = ''
    priority: int = 10
    trusted_russian: bool = False
    fallback_only: bool = False


def parse_m3u(
    text,
    base='',
    source='',
    default_user_agent='',
    default_referrer='',
    priority=10,
    trusted_russian=False,
    fallback_only=False
):
    current = None

    for line in text.splitlines():
        line = line.strip()

        if line.startswith('#EXTINF:'):
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
                name=name,
                url='',
                group=attrs.get('group-title', 'Разное'),
                tvg_id=attrs.get('tvg-id', ''),
                tvg_logo=attrs.get('tvg-logo', ''),
                language=attrs.get(
                    'tvg-language',
                    attrs.get('language', '')
                ),
                source=source,
                user_agent=default_user_agent,
                referrer=default_referrer,
                priority=int(priority or 10),
                trusted_russian=bool(trusted_russian),
                fallback_only=bool(fallback_only)
            )

        elif current and line.startswith('#EXTVLCOPT:'):
            current.restricted = True
            option = line[len('#EXTVLCOPT:'):]

            if option.lower().startswith('http-user-agent='):
                current.user_agent = safe_header_value(
                    option.split('=', 1)[1]
                )

            elif option.lower().startswith(
                ('http-referrer=', 'http-referer=')
            ):
                current.referrer = safe_referrer(
                    option.split('=', 1)[1]
                )

            else:
                current.unsafe_options = True

        elif current and line.startswith(
            ('#KODIPROP:', '#EXTHTTP:')
        ):
            current.restricted = True
            current.unsafe_options = True

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

        if langs and not langs & RUSSIAN:
            return None

        canonical = self.names.get(
            norm(entry.name)
        )

        if canonical:
            return norm(canonical), canonical

        if entry.tvg_id.casefold() in self.ids:
            name = (
                entry.name
                if re.search(r'\+\d', entry.name)
                else self.ids[entry.tvg_id.casefold()]
            )

            return norm(name), name

        if langs & RUSSIAN:
            return norm(entry.name), entry.name

        source = (entry.source or '').casefold()

        trusted_russian_source = (
            entry.trusted_russian
            or source.startswith('[dmi3y-tv]')
            or source.startswith('[loganet]')
            or source.startswith('[static]')
            or 'твое тв' in source
            or 'твоё тв' in source
        )

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
        line.strip()
        for line in text.splitlines()
        if line.strip()
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
                '1',
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
            or max(frames, default=0) < 5
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


def check_stream(
    url,
    fetcher,
    depth=0,
    user_agent='',
    referrer=''
):
    if depth > 3:
        raise Reject('hls_depth')

    kwargs = {
        'user_agent': user_agent,
        'referrer': referrer,
    }

    # First request is intentionally small.  It is enough to identify HLS
    # manifests or a transport stream without downloading megabytes per URL.
    data, final = fetcher.get(
        url,
        sample=True,
        limit=MANIFEST_SAMPLE_BYTES,
        **kwargs
    )

    if data.lstrip(
        b'\xef\xbb\xbf \r\n'
    ).startswith(b'#EXTM3U'):

        # The first response already contains the manifest.  Do not download
        # the same manifest a second time.
        variants, segments, init = hls_plan(
            decode(data),
            final
        )

        if variants:
            for variant in variants:
                try:
                    return check_stream(
                        variant,
                        fetcher,
                        depth + 1,
                        user_agent,
                        referrer
                    )
                except Reject:
                    pass

            raise Reject('no_working_variant')

        sample_parts = []

        if init:
            sample_parts.append(
                fetcher.get(
                    init,
                    sample=True,
                    limit=INIT_SAMPLE_BYTES,
                    **kwargs
                )[0]
            )

        # Two recent media fragments are enough to verify that both the
        # manifest and actual media are alive.  Only a small prefix of each
        # fragment is downloaded; ffprobe/ffmpeg can validate partial TS/fMP4
        # data and this reduces daily traffic by several times.
        for segment in segments[-2:]:
            sample_parts.append(
                fetcher.get(
                    segment,
                    sample=True,
                    limit=SEGMENT_SAMPLE_BYTES,
                    **kwargs
                )[0]
            )

        data = b''.join(sample_parts)

    else:
        # Direct MPEG-TS streams are checked from the small initial sample.
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


GROUP_EMOJI = {
    'Общие': '📺',
    'Федеральные': '📺',
    'Детские': '🧸',
    'Спорт': '⚽',
    'Кино': '🎬',
    'Музыкальные': '🎵',
    'Музыка': '🎵',
    'Развлекательные': '🎭',
    'Познавательные': '🧠',
    'Новостные': '📰',
    'Новости': '📰',
    'Мужские': '👨',
    'Женские': '👩',
    '4K UHD': '✨',
    '4K / UHD': '✨',
    'Relax': '🧘',
    'Для животных': '🐾',
    'Камеры': '📷',
    'Беларусь': '🇧🇾',
    'Радио': '📻',
    'Взрослые': '🔞',
    '18+': '🔞',
    'Украина': '🇺🇦',
}


def group_key(value):
    return ' '.join(
        re.findall(
            r'[a-zа-яё0-9+]+',
            str(value or '').casefold()
        )
    )


def first_existing_group(groups, *names):
    by_key = {
        group_key(g): g
        for g in groups
    }

    for name in names:
        found = by_key.get(
            group_key(name)
        )

        if found:
            return found

    return None


def source_group_target(group, groups):
    """
    Map common source group names to the user's preferred order.txt groups.
    Only groups that actually exist in order.txt can be returned.
    """
    g = group_key(group)

    aliases = [
        (
            (
                'общие', 'general', 'russia', 'россия',
                'федеральные', 'федеральные каналы',
                'эфирные', 'regional', 'регионы'
            ),
            ('Общие', 'Федеральные')
        ),
        (
            (
                'детские', 'детское', 'kids', 'children',
                'cartoon', 'cartoons', 'мультфильмы', 'мульт'
            ),
            ('Детские',)
        ),
        (
            (
                'спорт', 'sports', 'sport', 'football',
                'футбол', 'хоккей'
            ),
            ('Спорт',)
        ),
        (
            (
                'кино', 'movies', 'movie', 'cinema', 'films',
                'film', 'сериалы', 'series', 'кинозалы'
            ),
            ('Кино',)
        ),
        (
            (
                'музыкальные', 'музыка', 'music', 'musical'
            ),
            ('Музыкальные', 'Музыка')
        ),
        (
            (
                'развлекательные', 'развлечения',
                'entertainment', 'юмор', 'шоу'
            ),
            ('Развлекательные',)
        ),
        (
            (
                'познавательные', 'документальные',
                'documentary', 'science', 'history',
                'travel', 'путешествия'
            ),
            ('Познавательные',)
        ),
        (
            (
                'новостные', 'новости', 'news',
                'information', 'информационные'
            ),
            ('Новостные', 'Новости')
        ),
        (
            (
                'мужские', 'men', 'auto', 'авто',
                'охота', 'рыбалка'
            ),
            ('Мужские',)
        ),
        (
            (
                'женские', 'women', 'woman', 'fashion',
                'мода', 'кухня', 'food'
            ),
            ('Женские',)
        ),
        (
            (
                '4k uhd', '4k', 'uhd', 'ultra hd'
            ),
            ('4K UHD', '4K / UHD')
        ),
        (
            (
                'relax', 'релакс', 'lounge'
            ),
            ('Relax',)
        ),
        (
            (
                'для животных', 'animals', 'animal',
                'pets', 'животные'
            ),
            ('Для животных',)
        ),
        (
            (
                'камеры', 'camera', 'cameras',
                'webcam', 'webcams'
            ),
            ('Камеры',)
        ),
        (
            (
                'беларусь', 'belarus', 'by'
            ),
            ('Беларусь',)
        ),
        (
            (
                'радио', 'radio'
            ),
            ('Радио',)
        ),
        (
            (
                'взрослые', 'adult', '18+', 'xxx'
            ),
            ('Взрослые', '18+')
        ),
        (
            (
                'украина', 'ukraine', 'ua'
            ),
            ('Украина',)
        ),
    ]

    for source_names, targets in aliases:
        if g in source_names:
            target = first_existing_group(
                groups,
                *targets
            )

            if target:
                return target

    return None


def classify_new_channel(entry, groups):
    """
    Put channels absent from order.txt into one of the preferred groups.

    Priority:
      1. trustworthy source group-title;
      2. channel-name heuristics;
      3. "Общие" as the safe default.

    This keeps the old playlist structure while still allowing genuinely new
    channels to appear automatically.
    """
    direct = source_group_target(
        entry.group,
        groups
    )

    if direct:
        return direct

    name = group_key(entry.name)
    source_group = group_key(entry.group)
    haystack = f'{name} {source_group}'

    rules = [
        (
            (
                r'\b18\+\b', r'\badult\b', r'\bxxx\b',
                r'эрот', r'playboy', r'ночн'
            ),
            ('Взрослые', '18+')
        ),
        (
            (
                r'\bradio\b', r'\bfm\b', r'радио'
            ),
            ('Радио',)
        ),
        (
            (
                r'webcam', r'\bcamera\b', r'камер',
                r'онлайн камера'
            ),
            ('Камеры',)
        ),
        (
            (
                r'беларус', r'белорус', r'\bbelarus\b',
                r'\bбт\b'
            ),
            ('Беларусь',)
        ),
        (
            (
                r'украин', r'\bukraine\b', r'\bua\b'
            ),
            ('Украина',)
        ),
        (
            (
                r'\b4k\b', r'\buhd\b', r'ultra hd'
            ),
            ('4K UHD', '4K / UHD')
        ),
        (
            (
                r'мульт', r'cartoon', r'\bkids?\b',
                r'детск', r'child', r'baby',
                r'никелодеон', r'nick jr'
            ),
            ('Детские',)
        ),
        (
            (
                r'спорт', r'\bsport\b', r'футбол',
                r'football', r'хокке', r'баскет',
                r'волейбол', r'бокс', r'\bmma\b',
                r'fight', r'матч!', r'кхл'
            ),
            ('Спорт',)
        ),
        (
            (
                r'музык', r'\bmusic\b', r'\bmuz\b',
                r'mtv', r'шanson', r'шансон',
                r'клип', r'hit music'
            ),
            ('Музыкальные', 'Музыка')
        ),
        (
            (
                r'животн', r'\banimal\b', r'\bpets?\b',
                r'zoo', r'собак', r'кошк'
            ),
            ('Для животных',)
        ),
        (
            (
                r'\brelax\b', r'релакс', r'\blounge\b',
                r'myzen', r'my zen', r'meditat'
            ),
            ('Relax',)
        ),
        (
            (
                r'новост', r'\bnews\b', r'\brbk\b',
                r'\bрбк\b', r'известия', r'\brtvi\b',
                r'euronews', r'24 news'
            ),
            ('Новостные', 'Новости')
        ),
        (
            (
                r'кино', r'\bcinema\b', r'\bmovie\b',
                r'\bfilm\b', r'сериал', r'дорама',
                r'комедия', r'триллер', r'ужас',
                r'романтич', r'детектив'
            ),
            ('Кино',)
        ),
        (
            (
                r'охот', r'рыбал', r'оруж', r'авто',
                r'\bauto\b', r'motor', r'мужск'
            ),
            ('Мужские',)
        ),
        (
            (
                r'женск', r'\bfashion\b', r'мода',
                r'кухн', r'\bfood\b', r'домашн',
                r'здоровье'
            ),
            ('Женские',)
        ),
        (
            (
                r'познав', r'документ', r'discovery',
                r'history', r'science', r'national geographic',
                r'географ', r'путеше', r'\btravel\b',
                r'истори', r'наука', r'природ'
            ),
            ('Познавательные',)
        ),
        (
            (
                r'юмор', r'шоу', r'развлек',
                r'entertainment', r'comedy central',
                r'пятница', r'суббота'
            ),
            ('Развлекательные',)
        ),
    ]

    for patterns, targets in rules:
        if any(
            re.search(
                pattern,
                haystack,
                re.IGNORECASE
            )
            for pattern in patterns
        ):
            target = first_existing_group(
                groups,
                *targets
            )

            if target:
                return target

    return (
        first_existing_group(
            groups,
            'Общие',
            'Федеральные'
        )
        or (
            groups[0]
            if groups
            else 'Разное'
        )
    )


def decorate_group(group):
    group = clean(group or 'Разное')

    # Keep already decorated names as-is.
    if group and ord(group[0]) > 0x2500:
        return group

    emoji = GROUP_EMOJI.get(
        group
    )

    if emoji:
        return f'{emoji} {group}'

    g = group.casefold()

    rules = [
        (('федерал', 'общ'), '📺'),
        (('новост', 'news'), '📰'),
        (('кино', 'фильм', 'сериал'), '🎬'),
        (('дет', 'мульт', 'kids'), '🧸'),
        (('спорт', 'футбол', 'хоккей', 'sport'), '⚽'),
        (('музык', 'music'), '🎵'),
        (('познав', 'документ', 'history', 'science'), '🧠'),
        (('развлек', 'юмор', 'шоу'), '🎭'),
        (('4k', 'uhd'), '✨'),
        (('радио',), '📻'),
        (('живот', 'animal', 'pets'), '🐾'),
        (('камер', 'webcam'), '📷'),
        (('relax', 'релакс'), '🧘'),
        (('беларус',), '🇧🇾'),
        (('украин',), '🇺🇦'),
        (('18+', 'взросл', 'adult'), '🔞'),
    ]

    for needles, fallback_emoji in rules:
        if any(
            needle in g
            for needle in needles
        ):
            return f'{fallback_emoji} {group}'

    return f'📦 {group}'


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

    resolved_groups = {}

    for key, entry in entries.items():
        if key in lookup:
            # Existing channels keep exactly the group and order from the
            # user's historical playlist.
            group = lookup[key][0]

        else:
            # A genuinely new channel is automatically fitted into the same
            # group structure instead of creating a new "Разное" bucket.
            group = classify_new_channel(
                entry,
                groups
            )

        resolved_groups[key] = group

    def sort_key(item):
        key, entry = item
        group = resolved_groups[key]

        return (
            groups.index(group)
            if group in groups
            else len(groups),

            lookup.get(
                key,
                ('', 100000)
            )[1],

            natural(entry.name)
        )

    for key, entry in sorted(
        entries.items(),
        key=sort_key
    ):
        url_policy(entry.url)

        if entry.url in seen:
            continue

        seen.add(entry.url)

        group = decorate_group(
            resolved_groups[key]
        )

        logo_attr = (
            f'tvg-logo="{clean(entry.tvg_logo)}" '
            if entry.tvg_logo
            else ''
        )

        out.append(
            f'#EXTINF:-1 '
            f'tvg-id="{clean(entry.tvg_id)}" '
            f'{logo_attr}'
            f'tvg-language="Russian" '
            f'group-title="{group}",'
            f'{clean(entry.name)}'
        )

        if entry.user_agent:
            out.append(
                '#EXTVLCOPT:http-user-agent='
                + safe_header_value(
                    entry.user_agent
                )
            )

        if entry.referrer:
            out.append(
                '#EXTVLCOPT:http-referrer='
                + safe_referrer(
                    entry.referrer
                )
            )

        out.append(entry.url)

    return '\n'.join(out) + '\n'


def load_fixed_prefix(path):
    if not path:
        return ''

    p = Path(path)

    if not p.is_file():
        raise Reject('fixed_prefix_missing')

    text = p.read_text(
        encoding='utf-8-sig'
    ).strip()

    if not text.startswith('#EXTM3U'):
        raise Reject('fixed_prefix_not_m3u')

    return text + '\n'


def combine_fixed_and_dynamic(fixed_text, dynamic_text):
    if not fixed_text:
        return dynamic_text

    dynamic_lines = dynamic_text.splitlines()

    if (
        dynamic_lines
        and dynamic_lines[0].startswith('#EXTM3U')
    ):
        dynamic_lines = dynamic_lines[1:]

    body = '\n'.join(dynamic_lines).strip()

    if body:
        return fixed_text.rstrip() + '\n' + body + '\n'

    return fixed_text


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


def merge_extra_config(cfg, config_path):
    extra_path = cfg.get(
        'extra_sources_file',
        'extra_sources.json'
    )

    p = Path(config_path).resolve().parent / extra_path

    if not p.is_file():
        return cfg

    extra = json.loads(
        p.read_text('utf-8')
    )

    merged = dict(cfg)

    merged['sources'] = list(
        cfg.get('sources', [])
    ) + list(
        extra.get('sources', [])
    )

    if extra.get('fixed_prefix'):
        merged['fixed_prefix'] = str(
            Path(config_path).resolve().parent
            / extra['fixed_prefix']
        )

    return merged


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

    cfg = merge_extra_config(
        cfg,
        args.config
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
        'working_channels': 0,
        'fixed_channels': 0,
        'verified_dynamic_channels': 0,
    }

    rejected = Counter()
    workers = cfg.get(
        'workers',
        8
    )

    source_by_url = {}

    for source in cfg.get(
        'sources',
        []
    ):
        if (
            isinstance(source, dict)
            and source.get('url')
        ):
            source_by_url.setdefault(
                source['url'],
                source
            )

    def read_source(item):
        url = item['url']
        name = item.get(
            'name',
            url
        )

        try:
            raw, final = fetcher.get(
                url,
                strict=False,
                limit=item.get(
                    'max_source_bytes',
                    cfg.get(
                        'max_source_bytes',
                        8388608
                    )
                )
            )

            content = decode(raw)

            entries = (
                list(
                    parse_m3u(
                        content,
                        final,
                        name,
                        item.get(
                            'default_user_agent',
                            ''
                        ),
                        item.get(
                            'default_referrer',
                            ''
                        ),
                        item.get(
                            'priority',
                            10
                        ),
                        item.get(
                            'trusted_russian',
                            False
                        ),
                        item.get(
                            'fallback_only',
                            False
                        )
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
                source_by_url.setdefault(
                    url,
                    {
                        'url': url,
                        'name': (
                            urlsplit(page).hostname
                            + ' / discovered'
                        ),
                        'priority': 20,
                    }
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

    reference, reference_status = read_source({
        'url': cfg['language_reference'],
        'name': 'Russian-language metadata reference',
        'priority': 0,
    })

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
            source_by_url.values()
        ):
            entries.extend(result)
            report['sources'].append(
                status
            )

    report[
        'downloaded_entries'
    ] = len(entries)

    # Keep current-source entries separate from historical state.  The state
    # remains a useful candidate, but it must not make the archive look as if
    # a channel was found in a current primary source.
    current_entries = list(entries)
    state_entries = []

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

        for data in previous.values():
            # Backward compatibility with state files generated before
            # header/priority support existed.
            data = dict(data)
            data.setdefault('restricted', False)
            data.setdefault('unsafe_options', False)
            data.setdefault('user_agent', '')
            data.setdefault('referrer', '')
            data.setdefault('priority', 10)
            data.setdefault('tvg_logo', '')
            data.setdefault('trusted_russian', False)
            data.setdefault('fallback_only', False)
            state_entries.append(
                Entry(**data)
            )

    primary_candidates = defaultdict(dict)
    archive_candidates = defaultdict(dict)

    def add_candidate(target, e):
        try:
            url_policy(e.url)

            if e.unsafe_options:
                raise Reject(
                    'unsupported_custom_options'
                )

            match = language.identify(e)

            if not match:
                raise Reject(
                    'language_unknown_or_non_russian'
                )

            key, canonical = match
            e.name = canonical

            candidate_key = (
                e.url,
                e.user_agent,
                e.referrer
            )

            target[key].setdefault(
                candidate_key,
                e
            )

            return key

        except Reject as exc:
            rejected[str(exc)] += 1
            return None

    # Current primary sources define what is already "found" today.
    current_primary_keys = set()

    for e in current_entries:
        if e.fallback_only:
            add_candidate(archive_candidates, e)
        else:
            key = add_candidate(primary_candidates, e)
            if key:
                current_primary_keys.add(key)

    # Historical working URLs remain candidates, but they do not block a
    # genuinely missing channel from being supplied by the archive.
    for e in state_entries:
        add_candidate(primary_candidates, e)

    candidates = defaultdict(dict)

    for key, values in primary_candidates.items():
        candidates[key].update(values)

    archive_added_channels = 0
    archive_added_urls = 0

    for key, values in archive_candidates.items():
        if key in current_primary_keys:
            continue

        before = len(candidates[key])
        candidates[key].update(values)
        added = len(candidates[key]) - before

        if added:
            archive_added_channels += 1
            archive_added_urls += added

    report['primary_candidate_channels'] = len(current_primary_keys)
    report['archive_candidate_channels'] = len(archive_candidates)
    report['archive_added_channels'] = archive_added_channels
    report['archive_added_urls'] = archive_added_urls

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
                    'primary_candidate_channels',
                    'archive_candidate_channels',
                    'archive_added_channels',
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

    checked = {}
    checked_lock = threading.Lock()

    def check_once(entry):
        cache_key = (
            entry.url,
            entry.user_agent,
            entry.referrer
        )

        with checked_lock:
            owner = (
                cache_key not in checked
            )

            if owner:
                checked[
                    cache_key
                ] = Future()

            future = checked[
                cache_key
            ]

        if owner:
            try:
                for attempt in range(2):
                    try:
                        if (
                            entry.user_agent
                            or entry.referrer
                        ):
                            result = check_stream(
                                entry.url,
                                fetcher,
                                user_agent=entry.user_agent,
                                referrer=entry.referrer
                            )
                        else:
                            result = check_stream(
                                entry.url,
                                fetcher
                            )

                        future.set_result(
                            result
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
        key, candidate_map = item

        prior_url = previous.get(
            key,
            {}
        ).get('url')

        ordered = sorted(
            candidate_map.values(),
            key=lambda e: (
                e.priority,
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
                    entry
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

    dynamic_text = render(
        selected,
        cfg.get(
            'order_file',
            ''
        ),
        cfg['russian_aliases']
    )

    try:
        fixed_text = load_fixed_prefix(
            cfg.get(
                'fixed_prefix',
                ''
            )
        )
    except Reject as exc:
        report.update(
            status=str(exc),
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
            'Fixed prefix unavailable; '
            'existing output was not replaced.'
        )

        return 1

    text = combine_fixed_and_dynamic(
        fixed_text,
        dynamic_text
    )

    fixed_channels = fixed_text.count(
        '#EXTINF:'
    )

    dynamic_channels = dynamic_text.count(
        '#EXTINF:'
    )

    total_channels = text.count(
        '#EXTINF:'
    )

    report[
        'downloaded_bytes'
    ] = budget.bytes

    report.update(
        status=(
            'ok'
            if selected or fixed_channels
            else 'failed_no_verified_streams'
        ),

        rejected=dict(rejected),

        fixed_channels=fixed_channels,

        verified_dynamic_channels=dynamic_channels,

        working_channels=total_channels,

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
        if total_channels
        else None
    )

    if total_channels:
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
                ],
                'fixed_channels': report[
                    'fixed_channels'
                ],
                'verified_dynamic_channels': report[
                    'verified_dynamic_channels'
                ],
            },
            ensure_ascii=False
        )
    )

    return (
        0
        if total_channels
        else 1
    )


if __name__ == '__main__':
    raise SystemExit(main())
