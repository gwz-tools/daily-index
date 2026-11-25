#!/usr/bin/env python3
"""Mirror only the verified playlist to the dedicated Yandex Disk app folder."""
import hashlib
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from publish_dropbox import load_config, verified_data

API = "https://cloud-api.yandex.net/v1/disk"
PATH = "app:/playlistRU.m3u"


class MirrorError(Exception):
    pass


def https(url):
    p = urlsplit(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        raise MirrorError("Invalid HTTPS link from Yandex")
    return url


def request(method, url, token=None, data=None, missing=False, max_bytes=1024*1024):
    headers = {}
    if token:
        # Credentials are never sent to upload or public download hosts.
        if urlsplit(url).hostname != "cloud-api.yandex.net":
            raise MirrorError("Unexpected authenticated endpoint")
        headers["Authorization"] = "OAuth " + token
    if data is not None:
        headers["Content-Type"] = "application/octet-stream"
    for attempt in range(3):
        try:
            with urlopen(Request(https(url), data=data, headers=headers, method=method), timeout=60) as response:
                https(response.geturl())
                raw = response.read(max_bytes+1)
                if len(raw) > max_bytes:
                    raise MirrorError("Yandex response exceeds expected size")
                return raw
        except HTTPError as exc:
            code = exc.code
            exc.close()
            if code == 404 and missing:
                return None
            if code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2**attempt)
                continue
            raise MirrorError("Yandex HTTP " + str(code) + " (" + method + ")") from None
        except (URLError, TimeoutError, OSError):
            if attempt < 2:
                time.sleep(2**attempt)
                continue
            raise MirrorError("Yandex network error (" + method + ")") from None


def api(method, route, token=None, missing=False, **params):
    raw = request(method, API + route + "?" + urlencode(params), token=token, missing=missing)
    return None if raw is None else json.loads(raw or b"{}")


def publish(data, token):
    digest = hashlib.sha256(data).hexdigest()
    old = api("GET", "/resources", token, missing=True, path=PATH)
    old_url = (old or {}).get("public_url")
    if not old or old.get("sha256") != digest or old.get("size") != len(data):
        link = api("GET", "/resources/upload", token, path=PATH, overwrite="true")
        request("PUT", https(link["href"]), data=data)
    # Metadata can lag the upload briefly.
    for attempt in range(5):
        meta = api("GET", "/resources", token, path=PATH)
        if meta.get("sha256") == digest and meta.get("size") == len(data):
            break
        time.sleep(2)
    else:
        raise MirrorError("Yandex metadata hash/size mismatch")
    public_url = meta.get("public_url")
    if old_url and public_url != old_url:
        raise MirrorError("Existing public link changed; check Yandex Disk sharing")
    if not public_url:
        api("PUT", "/resources/publish", token, path=PATH)
        for attempt in range(5):
            meta = api("GET", "/resources", token, path=PATH)
            public_url = meta.get("public_url")
            if public_url:
                break
            time.sleep(2)
    if not public_url:
        raise MirrorError("No public URL returned")
    # Verify the anonymous download, not the HTML sharing page.
    https(public_url)
    link = api("GET", "/public/resources/download", public_key=public_url)
    actual = request("GET", https(link["href"]), max_bytes=len(data))
    if actual != data:
        raise MirrorError("Anonymous download differs from verified playlist")
    return public_url


def main():
    token = os.environ.get("YANDEX_DISK_TOKEN", "").strip()
    if not token:
        raise MirrorError("Missing YANDEX_DISK_TOKEN")
    data = verified_data(load_config())
    if not data.decode("utf-8-sig").startswith("#EXTM3U"):
        raise MirrorError("Invalid M3U header")
    public_url = publish(data, token)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("### Yandex Disk mirror verified\n\n")
            f.write("Public playlist: " + public_url + "\n\n")
            f.write("SHA-256: " + hashlib.sha256(data).hexdigest() + "\n")
    print("Yandex Disk: anonymous M3U download verified; " + str(len(data)) + " bytes")


if __name__ == "__main__":
    try:
        main()
    except MirrorError as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        # Do not print token, temporary signed URLs or remote response bodies.
        raise SystemExit("Yandex publication failed; check API access and verified local build") from None
