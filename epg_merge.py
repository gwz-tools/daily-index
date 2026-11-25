#!/usr/bin/env python3
"""Build a compact, playlist-specific XMLTV guide; publish independently of M3U refresh."""
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from epg_cache import date, atomic
import io
from urllib.error import HTTPError, URLError
from publish_dropbox import access_token, metadata, request_json, content_hash, PublishError, load_config

SOURCES = [
    ("IPTVX", "https://iptvx.one/EPG_LITE"),
    ("EPG.ONE HTTPS", "https://epg.one/ru.xml.gz"),
]
LIMIT = 48 * 1024 * 1024

def download(url, limit):
    # Some published guide URLs are HTTP-only. No credentials are sent.
    # Accept only the configured public endpoints and bounded XML data.
    request = Request(url, headers={"User-Agent":"GWZ-EPG/1.0","Accept":"application/xml,application/gzip,*/*"})
    with urlopen(request, timeout=35) as resp:
        data=resp.read(limit+1)
    if len(data)>limit:
        raise ValueError("compressed source too large")
    if data.startswith(b'\x1f\x8b'):
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as z:
            data=z.read(limit*8+1)
    if len(data)>limit*8:
        raise ValueError("uncompressed source too large")
    # XMLTV providers often declare the standard external DTD. ElementTree
    # does not need it; remove only the simple declaration, never load it.
    # Reject all internal subsets and entity declarations.
    if re.search(br'<!ENTITY\b', data, re.I):
        raise ValueError("XML entity declaration forbidden")
    data = re.sub(br'<!DOCTYPE\s+tv(?:\s+(?:SYSTEM\s+["\'][^"\'<>\[\]]+["\']|PUBLIC\s+["\'][^"\'<>\[\]]+["\']\s+["\'][^"\'<>\[\]]+["\']))?\s*>', b'', data, flags=re.I)
    if re.search(br'<!DOCTYPE\b', data, re.I):
        raise ValueError("Unsupported XML DTD")

    return data

def norm(value):
    return " ".join((value or "").casefold().split())

def quality_alias(value):
    # Strip only a final resolution label; preserve time shifts, city, language and channel number.
    return re.sub(r"\s+(?:full\s*hd|uhd|fhd|hd|sd|4k)$", "", norm(value)).strip()

def targets(text):
    rows = {}
    for line in text.splitlines():
        if not line.startswith("#EXTINF:"):
            continue
        # The comma separator must be outside quoted attributes.
        parts = re.split(r',(?=(?:[^"]*"[^"]*")*[^"]*$)', line, maxsplit=1)
        if len(parts) != 2:
            continue
        header, name = parts
        attr = lambda k: (re.search(r'\b'+k+r'="([^"]*)"', header) or [None, ""])[1]
        group = attr("group-title")
        if "Камеры" in group or "Радио" in group:
            continue
        cid = attr("tvg-id").strip()
        title = attr("tvg-name").strip() or name.strip()
        if not title:
            continue
        key = cid or "gwz-" + hashlib.sha256(norm(title).encode()).hexdigest()[:24]
        row = rows.setdefault(key, {"id": cid, "names": set()})
        row["names"].update(x for x in (title, name.strip()) if x)
    return rows

def merge(rows, now, fetch=download, sources=SOURCES):
    lower, upper = now-timedelta(days=1), now+timedelta(days=3)
    output = ET.Element("tv", {"generator-info-name":"GWZ EPG"})
    selected = {}
    status = []
    for label, url in sources:
        report = {"name":label, "status":"error", "matched":0}
        try:
            root = ET.fromstring(fetch(url, LIMIT))
            if root.tag != "tv":
                raise ValueError("not XMLTV")
            channels = {c.get("id"):c for c in root.findall("channel") if c.get("id")}
            names = defaultdict(set)
            for cid, c in channels.items():
                for n in c.findall("display-name"):
                    if norm(n.text):
                        names[norm(n.text)].add(cid)
            aliases = defaultdict(set)
            for cid, c in channels.items():
                for n in c.findall("display-name"):
                    alias = quality_alias(n.text)
                    if alias:
                        aliases[alias].add(cid)
            wanted = defaultdict(list)
            for key, row in rows.items():
                # Exact IDs win, otherwise only unambiguous exact names.
                candidate = row["id"] if row["id"] in channels else None
                if candidate is None:
                    matches = set().union(*(names.get(norm(n),set()) for n in row["names"]))
                    if len(matches)==1:
                        candidate = next(iter(matches))
                    elif not matches:
                        compatible = set().union(*(aliases.get(quality_alias(n),set()) for n in row["names"]))
                        if len(compatible)==1:
                            candidate = next(iter(compatible))
                if candidate:
                    wanted[candidate].append(key)
            programmes = defaultdict(list)
            for p in root.findall("programme"):
                cid=p.get("channel")
                if cid not in wanted or not p.findtext("title"):
                    continue
                try:
                    start,stop=date(p.get("start")),date(p.get("stop"))
                except ValueError:
                    continue
                if start<stop and start<upper and stop>lower:
                    programmes[cid].append((start,stop,p))
            for cid, keys in wanted.items():
                entries=programmes.get(cid, [])
                if not entries or not any(stop>now and start<now+timedelta(hours=24) for start,stop,_ in entries):
                    continue
                for key in keys:
                    old=selected.get(key)
                    if old and old[0]>=sum(stop>now for _,stop,_ in entries):
                        continue
                    selected[key]=(sum(stop>now for _,stop,_ in entries),channels[cid],entries)
                    report["matched"]+=1
            report["status"]="ok"
        except (OSError, ValueError, ET.ParseError, EOFError) as exc:
            report["error"]=type(exc).__name__
            if isinstance(exc, HTTPError): report["http_status"]=exc.code
            elif isinstance(exc, URLError): report["reason"]=str(exc.reason)[:160]
            elif isinstance(exc, (ValueError, ET.ParseError)): report["reason"]=str(exc)[:160]
        status.append(report)
    if len(selected)<min(30,len(rows)) or not selected:
        print(json.dumps({"target_keys":len(rows),"covered_keys":len(selected),"sources":status},ensure_ascii=False))
        raise ValueError("insufficient fresh coverage: previous guide retained")
    for key,(_,channel,entries) in sorted(selected.items()):
        c=ET.SubElement(output,"channel",{"id":key})
        for name in sorted(rows[key]["names"]):
            ET.SubElement(c,"display-name").text=name
        icon=channel.find("icon")
        if icon is not None and (icon.get("src") or "").startswith(("http://","https://")):
            ET.SubElement(c,"icon",{"src":icon.get("src")})
    count=0
    for key,(_,channel,entries) in sorted(selected.items()):
        seen=set()
        for start,stop,p in sorted(entries,key=lambda e:(e[0],e[1])):
            if (start,stop) in seen:
                continue
            seen.add((start,stop))
            q=ET.SubElement(output,"programme",{"channel":key,"start":start.strftime("%Y%m%d%H%M%S +0000"),"stop":stop.strftime("%Y%m%d%H%M%S +0000")})
            for tag in ("title","sub-title","category","episode-num"):
                el=p.find(tag)
                if el is not None and el.text:
                    ET.SubElement(q,tag,dict(el.attrib)).text=el.text
            count+=1
    return gzip.compress(ET.tostring(output,encoding="utf-8",xml_declaration=True),compresslevel=6,mtime=0), {
        "updated_at":now.isoformat(),"target_keys":len(rows),"covered_keys":len(selected),"programmes":count,"sources":status
    }

def direct_link():
    link=os.environ.get("GWZ_EPG_URL","").strip()
    if not link:
        return None
    u=urlsplit(link)
    if u.scheme!="https" or u.hostname not in ("www.dropbox.com","dropbox.com","dl.dropboxusercontent.com"):
        raise ValueError("GWZ_EPG_URL must be a Dropbox HTTPS link")
    query=[(k,v) for k,v in parse_qsl(u.query) if k not in ("dl","raw")]+[("dl","1")]
    return urlunsplit((u.scheme,u.netloc,u.path,urlencode(query),""))

def publish(data, report, now):
    token=access_token()
    folder=load_config()["dropbox"]["path"].rsplit("/",1)[0]
    path=folder+"/gwz-epg.xml.gz"
    auth={"Authorization":"Bearer "+token,"Content-Type":"application/json"}
    def api(endpoint,obj):
        return request_json("https://api.dropboxapi.com/2/"+endpoint,json.dumps(obj).encode(),auth)
    # Locate existing file via folder listing, not by treating arbitrary metadata errors as absence.
    listing=api("files/list_folder",{"path":folder})
    entries=listing["entries"]
    while listing.get("has_more"):
        listing=api("files/list_folder/continue",{"cursor":listing["cursor"]})
        entries+=listing["entries"]
    previous=next((e for e in entries if e.get("path_lower","").casefold()==path.casefold()),None)
    if previous:
        if previous.get(".tag")!="file":
            raise ValueError("EPG destination not a file")
        # Preserve the old guide if still useful and new coverage has collapsed.
        headers={"Authorization":"Bearer "+token,"Dropbox-API-Arg":json.dumps({"path":previous["id"]})}
        try:
            with urlopen(Request("https://content.dropboxapi.com/2/files/download",headers=headers,method="POST"),timeout=45) as resp:
                old=resp.read(LIMIT+1)
        except HTTPError as exc:
            if exc.code not in (401,403):
                raise
            exc.close()
            direct=direct_link()
            if not direct:
                report["publication"]="blocked_previous_file_read"
                raise PublishError("Dropbox token cannot read previous guide. Set GWZ_EPG_URL or grant files.content.read; previous guide untouched.") from None
            with urlopen(Request(direct,headers={"User-Agent":"GWZ-EPG/1.0"}),timeout=45) as resp:
                old=resp.read(LIMIT+1)
            if content_hash(old)!=previous.get("content_hash"):
                raise PublishError("Public link does not match previous Dropbox file; previous guide untouched.")
        if len(old)>LIMIT:
            raise ValueError("previous guide too large")
        oldroot=ET.fromstring(gzip.decompress(old))
        live=set()
        for p in oldroot.findall("programme"):
            try:
                if date(p.get("stop"))>now and date(p.get("start"))<now+timedelta(hours=24):
                    live.add(p.get("channel"))
            except ValueError:
                pass
        if report["covered_keys"]<len(live)*0.7:
            raise ValueError("fresh coverage dropped >30%; previous file retained")
    mode={".tag":"update","update":previous["rev"]} if previous else {".tag":"add"}
    arg={"path":path,"mode":mode,"autorename":False,"mute":True,"strict_conflict":True}
    uploaded=request_json("https://content.dropboxapi.com/2/files/upload",data,{
        "Authorization":"Bearer "+token,"Content-Type":"application/octet-stream",
        "Dropbox-API-Arg":json.dumps(arg,ensure_ascii=True,separators=(",",":"))
    })
    remote=metadata(token,uploaded["id"])
    if remote.get("content_hash")!=content_hash(data) or remote.get("size")!=len(data) or (previous and remote.get("id")!=previous["id"]):
        raise ValueError("EPG upload not verified")
    # Existing token uploads files but sharing/list_shared_links returned HTTP 400.
    # Set a manually created Dropbox download link once in Actions Secrets.
    print("Dropbox EPG file uploaded and verified by identity, size and content hash.")
    direct=direct_link()
    if not direct:
        report["publication"]="uploaded_link_not_configured"
        print("::warning::Set GWZ_EPG_URL to the Dropbox download link to verify device access.")
        return
    with urlopen(Request(direct,headers={"User-Agent":"GWZ-EPG/1.0"}),timeout=60) as resp:
        received=resp.read(len(data)+1)
    if received!=data:
        raise ValueError("shared download differs from verified XMLTV")
    report["publication"]="download_verified"
    print("Dropbox EPG upload and shared-link download verified.")
    # Link is obtained later from Dropbox sharing controls; no public repo link artifact.

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--publish",action="store_true")
    args=ap.parse_args()
    now=datetime.now(timezone.utc)
    rows=targets(Path("playlistRU.m3u").read_text("utf-8-sig"))
    data,report=merge(rows,now)
    report["bytes_gzip"]=len(data)
    atomic("epg-output/gwz-epg.xml.gz",data)
    atomic("epg-output/status.json",(json.dumps(report,ensure_ascii=False,indent=2)+"\n").encode())
    print(json.dumps(report,ensure_ascii=False))
    if args.publish:
        try:
            publish(data,report,now)
        finally:
            atomic("epg-output/status.json",(json.dumps(report,ensure_ascii=False,indent=2)+"\n").encode())

if __name__=="__main__":
    main()
