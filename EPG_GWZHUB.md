# GWZHUB EPG cache — staging checklist

The current production M3U retains its three original `url-tvg` URLs until
the server endpoint and coverage have been checked. Do not point clients at a
file that has not been published. Dynamic entries already carry `tvg-id`,
`tvg-name` and, where a source provides it, `tvg-logo`.

1. Back up the current M3U and any existing EPG file on GWZHUB. Copy
   `epg_cache.py`, `epg_sources.json` and the **current** `playlistRU.m3u` to
   the host. The playlist must be refreshed after each GitHub build; otherwise
   new channels will not be included in the guide.
2. Run `python3 epg_cache.py --config epg_sources.json --playlist playlistRU.m3u
   --output /var/www/epg/guide.xml.gz --status /var/lib/gwz-epg/status.json`.
   Inspect `covered_ids`, `playlist_ids`, and source errors. First download
   from each provider must be verified from GWZHUB; URLs are not guaranteed.
3. Serve the file on a stable public HTTPS URL. For `.xml.gz`, set
   `Content-Type: application/gzip` and **do not** also set
   `Content-Encoding: gzip` unless serving it transparently as `.xml`.
   Check with `curl -I` and by importing in the actual TV application.
4. Only after the above works, replace the M3U header's `url-tvg` with the
   working GWZHUB URL. Keep an external EPG URL as a documented fallback,
   not as an additional client download. Update Dropbox through the normal
   validated GitHub pipeline; never publish a manually edited output.
5. Schedule the script every six hours using a timer with a 15-minute timeout.
   Alert when `updated_at` is over 24 hours old or coverage drops sharply.
   A failed fetch or zero usable programmes leaves the previous file intact.
   Keep a copy of the previous *good* guide off-host as a second fallback.

No TV logos are embedded as binary image data in the M3U. `tvg-logo` is an
ordinary URL; source icons should be validated separately before filling
missing attributes. Do not guess a logo from a similarly named channel.
