#!/usr/bin/env python3
"""Update the existing Dropbox file only from a recent, verified build."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from refresh import parse_m3u, url_policy


class PublishError(Exception):
    pass


def content_hash(data):
    block = 4 * 1024 * 1024
    return hashlib.sha256(b''.join(hashlib.sha256(data[i:i+block]).digest() for i in range(0, len(data), block))).hexdigest()


def request_json(url, data, headers):
    # Never log requests, tokens or Dropbox response bodies containing credentials.
    for attempt in range(3):
        try:
            with urlopen(Request(url, data=data, headers=headers, method='POST'), timeout=30) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            code = exc.code
            exc.close()
            if code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise PublishError('Dropbox HTTP ' + str(code)) from None
        except (URLError, TimeoutError, json.JSONDecodeError):
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise PublishError('Dropbox network/response error') from None


def access_token():
    names = ('DROPBOX_APP_KEY', 'DROPBOX_APP_SECRET', 'DROPBOX_REFRESH_TOKEN')
    values = [os.environ.get(n, '').strip() for n in names]
    if not all(values):
        raise PublishError('Missing GitHub Actions secrets: ' + ', '.join(n for n, v in zip(names, values) if not v))
    data = urlencode(dict(grant_type='refresh_token', client_id=values[0], client_secret=values[1], refresh_token=values[2])).encode()
    response = request_json('https://api.dropboxapi.com/oauth2/token', data, {'Content-Type': 'application/x-www-form-urlencoded'})
    token = response.get('access_token')
    if not token:
        raise PublishError('Dropbox did not return an access token')
    # GitHub masks stored secrets; also mask this freshly issued short-lived token.
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        print('::add-mask::' + token)
    return token


def metadata(token, path):
    return request_json('https://api.dropboxapi.com/2/files/get_metadata', json.dumps({'path': path}).encode(), {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})


def verified_data(config):
    status = json.loads(Path(config['report']).read_text('utf-8'))
    data = Path(config['output']).read_bytes()
    if status.get('status') != 'ok' or status.get('playlist_sha256') != hashlib.sha256(data).hexdigest():
        raise PublishError('Playlist does not match a successful verified build')
    checked = datetime.fromisoformat(status['checked_at'])
    age = (datetime.now(timezone.utc) - checked).total_seconds()
    if not 0 <= age <= 7200:
        raise PublishError('Build is stale or has a future timestamp')
    text = data.decode('utf-8')
    entries = list(parse_m3u(text))
    if not entries or len(entries) != status['working_channels'] or sum(l.strip() == '#EXTM3U' for l in text.splitlines()) != 1:
        raise PublishError('Invalid playlist structure')
    for entry in entries:
        url_policy(entry.url)
        if entry.restricted or entry.language != 'Russian':
            raise PublishError('Invalid entry in verified playlist')
    return data


def publish(config, token, data):
    target = config['dropbox']
    expected_id = target['expected_file_id']
    current = metadata(token, expected_id)
    if current.get('id') != expected_id or current.get('.tag') != 'file':
        raise PublishError('Dropbox destination identity does not match')
    # Never silently create a second file under a misspelled path or different app.
    if current.get('path_lower', '').casefold() != target['path'].casefold():
        raise PublishError('Dropbox destination moved; update the configured path first')
    expected_hash = content_hash(data)
    if current.get('content_hash') == expected_hash:
        print('Dropbox already contains this verified build.')
        return
    arg = {'path': target['path'], 'mode': {'.tag': 'update', 'update': current['rev']}, 'autorename': False, 'mute': True, 'strict_conflict': True}
    try:
        uploaded = request_json('https://content.dropboxapi.com/2/files/upload', data, {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/octet-stream', 'Dropbox-API-Arg': json.dumps(arg, ensure_ascii=True, separators=(',', ':'))})
    except PublishError:
        # The server can accept an upload before a network failure. Read back the
        # content hash; never overwrite a concurrently changed revision blindly.
        uploaded = metadata(token, expected_id)
    if uploaded.get('id') != expected_id or uploaded.get('size') != len(data) or uploaded.get('content_hash') != expected_hash:
        raise PublishError('Dropbox upload was not confirmed; identity/hash mismatch')
    remote = metadata(token, expected_id)
    if remote.get('id') != expected_id or remote.get('content_hash') != expected_hash:
        raise PublishError('Dropbox read-back verification failed')
    print('Dropbox existing file updated and verified; shared link preserved.')


def main():
    config = json.loads(Path('sources.json').read_text('utf-8'))
    # A missing Dropbox connection is a failure, never a green skipped sync.
    data = verified_data(config)
    publish(config, access_token(), data)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (PublishError, ValueError, OSError) as exc:
        # PublishError strings contain no server response bodies or secrets.
        print(str(exc) if isinstance(exc, PublishError) else 'Invalid or missing local build data')
        raise SystemExit(1)
