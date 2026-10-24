#!/usr/bin/env python3
"""
SonyLIV -> auto-updating IPTV playlist (M3U). Free tier, no login, no premium.

Token acquisition is multi-strategy. Strategies are tried in order and the
first token that actually verifies against the CDN wins:

  A. apiv2 AGL VIDEOURL  -- 4 known-good donor assets x region x version
  B. sonyliv.com v4 web API  -- hlsUrl token from a live asset
  C. live-tv page scrape    -- any hdnea/hdntl string in the HTML
  D. community worker feed  -- last resort, also reveals current CDN host

HTTP errors are read from the BODY, never guessed from the status code.

USAGE
    python scraper.py --selftest --diag     # diagnose token acquisition
    python scraper.py -o sonyliv.m3u --check --prune --live
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    sys.exit("Missing dependency ->  python -m pip install requests")

# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
API_BASE   = "https://apiv2.sonyliv.com"
SITE       = "https://www.sonyliv.com"
TOKEN_PATH = "/AGL/1.4/A/ENG/WEB/ALL/GETTOKEN"
CDN_HOSTS = [
    "https://dishmt.slivcdn.com",
    "https://slivcdn.com",
    "https://sonylivmt.slivcdn.com",
]

# Known-good VOD assets (taken from yt-dlp's SonyLIV test cases -- far more
# reliable than guessing ids out of image paths). Tried in order.
DONOR_ASSETS = ["1000022678", "1000050121", "1000018779", "1000098925"]

# (region, api-version) combos for the VIDEOURL endpoint, best first.
REGION_VERSION = [("IN", "1.5"), ("IN", "1.6"), ("ALL", "1.5"),
                  ("IN", "1.4"), ("ALL", "1.6")]

WORKER_FEEDS = ["https://sliv.gramnaters.workers.dev/playlist.m3u"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# apiv2 wants a plain JSON accept + referer. No Origin (matches yt-dlp, and
# Akamai bot detection is pickier when extra headers are present).
API_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Referer": SITE,
}
WEB_HEADERS = {
    "User-Agent": UA,
    "Accept": "*/*",
    "Referer": SITE + "/",
    "Origin": SITE,
}

GROUP        = "SonyLIV"
REUSE_MARGIN = 3 * 3600
HTTP_TIMEOUT = 10
IST          = timezone(timedelta(hours=5, minutes=30))
DEBUG_FILE   = "sonyliv.debug.json"

VERBOSE = False
DIAG    = False

CHANNELS = [
    ("sony-hd",       "SONY HD",       "Entertainment", "Hindi",   "2011671/SETHD"),
    ("sab-hd",        "SAB HD",        "Entertainment", "Hindi",   "2011749/SABHD"),
    ("sony-marathi",  "SONY MARATHI",  "Entertainment", "Marathi", "2011740/SonyMarathi"),
    ("sony-pal",      "SONY PAL",      "Entertainment", "Hindi",   "2011741/SonyPalSD"),
    ("sony-aath",     "SONY AATH",     "Entertainment", "Bengali", "2011641/SonyAathSD"),
    ("sony-yay",      "SONY YAY",      "Kids",          "Hindi",   "2011746/SonyYaySD"),
    ("sony-max-hd",   "SONY MAX HD",   "Movies",        "Hindi",   "2011670/SonyMaxhd"),
    ("sony-max",      "SONY MAX",      "Movies",        "Hindi",   "2011745/SonyMaxSD"),
    ("sony-max2",     "SONY MAX2",     "Movies",        "Hindi",   "2011908/MAX2"),
    ("sony-wah",      "SONY WAH",      "Movies",        "Hindi",   "2011906/SonyWah"),
    ("sony-pix-hd",   "SONY PIX HD",   "Movies",        "Hindi",   "2011748/PIXHD"),
    ("sony-ten-1-hd", "SONY TEN 1 HD", "Sports",        "English", "2011747/TEN1HD"),
    ("sony-ten-1",    "SONY TEN 1",    "Sports",        "English", "2011739/TEN1SD"),
    ("sony-ten-2-hd", "SONY TEN 2 HD", "Sports",        "English", "2020434/TEN2HD"),
    ("sony-ten-2",    "SONY TEN 2",    "Sports",        "English", "2020590/TEN2SD"),
    ("sony-ten-3-hd", "SONY TEN 3 HD", "Sports",        "Hindi",   "2020591/TEN3HD"),
    ("sony-ten-3",    "SONY TEN 3",    "Sports",        "Hindi",   "2020592/TEN3SD"),
    ("sony-ten-4-hd", "SONY TEN 4 HD", "Sports",        "Telugu",  "2020589/ten4hd"),
    ("sony-ten-4",    "SONY TEN 4",    "Sports",        "Telugu",  "2020437/ten4sd"),
    ("sony-ten-5-hd", "SONY TEN 5 HD", "Sports",        "Hindi",   "2020593/SONYSIXHD"),
    ("sony-ten-5",    "SONY TEN 5",    "Sports",        "English", "2020594/SONYSIXSD"),
    ("bbc-earth",     "BBC EARTH",     "Infotainment",  "English", "2011907/SonyBBCEarthHD"),
]

TOKEN_RE = re.compile(r"\b(hdn(?:ea|tl))=([^&\s\"'<>]+)")
DEBUG: dict = {}


def log(msg: str) -> None:
    print(msg, flush=True)


def vlog(msg: str) -> None:
    if VERBOSE or DIAG:
        print(msg, flush=True)


# --------------------------------------------------------------------------- #
# http helpers
# --------------------------------------------------------------------------- #
def _get(url, headers=None, cookie=None, timeout=HTTP_TIMEOUT):
    h = dict(headers or API_HEADERS)
    if cookie:
        h["Cookie"] = cookie
    r = requests.get(url, headers=h, timeout=timeout)
    if DIAG:
        vlog(f"    GET {url} -> {r.status_code} {r.text[:140]!r}")
    return r


def _post(url, payload, timeout=HTTP_TIMEOUT):
    r = requests.post(url, json=payload, headers={**WEB_HEADERS,
                     "Content-Type": "application/json"}, timeout=timeout)
    if DIAG:
        vlog(f"    POST {url} -> {r.status_code} {r.text[:140]!r}")
    return r


def parse_token(raw: str):
    m = TOKEN_RE.search(raw or "")
    return (m.group(1), m.group(2)) if m else None


def token_expiry(value: str) -> int:
    m = re.search(r"exp=(\d+)", value)
    return int(m.group(1)) if m else 0


def body_msg(resp) -> str:
    """Pull the human message out of an error body instead of guessing."""
    try:
        data = resp.json()
        for key in ("message", "errorMessage", "resultObj", "error"):
            if isinstance(data.get(key), str):
                return data[key]
        return json.dumps(data)[:150]
    except Exception:
        return (resp.text or "")[:150].replace("\n", " ")


# --------------------------------------------------------------------------- #
# strategies  ->  return ((token_name, token_value), host_or_None) | None
# --------------------------------------------------------------------------- #
def strat_apiv2():
    r = _get(API_BASE + TOKEN_PATH)
    if r.status_code != 200:
        log(f"  [apiv2] GETTOKEN -> {r.status_code} {body_msg(r)}")
        return None
    jwt = (r.json() or {}).get("resultObj")
    if not isinstance(jwt, str) or not jwt:
        log(f"  [apiv2] GETTOKEN had no resultObj")
        return None

    last = None
    for asset in DONOR_ASSETS:
        for region, ver in REGION_VERSION:
            url = f"{API_BASE}/AGL/{ver}/A/ENG/WEB/{region}/CONTENT/VIDEOURL/VOD/{asset}"
            try:
                rr = _get(url, headers={**API_HEADERS, "security_token": jwt})
            except Exception as exc:
                last = f"{asset} {region}/{ver}: {exc}"
                continue
            if rr.status_code == 200:
                vurl = ((rr.json() or {}).get("resultObj") or {}).get("videoURL") or ""
                tok = parse_token(vurl)
                if tok:
                    log(f"  [apiv2] ok via {asset} {region}/v{ver}")
                    DEBUG["apiv2_asset"] = asset
                    return tok, None
                last = f"{asset} {region}/{ver}: no token in videoURL"
            else:
                last = f"{asset} {region}/v{ver}: {rr.status_code} {body_msg(rr)}"
                vlog(f"    {last}")
    log(f"  [apiv2] all combos failed. last: {last}")
    DEBUG["apiv2_last_error"] = last
    return None


SEARCH_BODY = {
    "detailsType": "basic",
    "searchSet": [{
        "pageSize": 40, "pageNumber": 0, "sortOrder": "START_DATE:DESC",
        "data": "all=classification:channel&all=type:live",
        "type": "search", "id": "live_channels", "itemsUsed": 0, "language": "",
    }],
    "deviceDetails": {"mfg": "WEB", "os": "others", "osVer": "XXX",
                      "model": "WEB", "deviceId": "a1b2c3d4-0001",
                      "platform": "web"},
    "isSearchable": True,
}


def strat_web_v4():
    r = _post(SITE + "/api/v4/vod/search", SEARCH_BODY)
    if r.status_code != 200:
        log(f"  [v4] search -> {r.status_code} {body_msg(r)}")
        return None
    payload = r.json() or {}
    node = payload.get("0") or payload
    ids = [i["id"] for i in (node.get("assets") or []) if i.get("id")]
    if not ids:
        log("  [v4] search returned no assets")
        return None

    for asset_id in ids[:8]:
        try:
            rr = _post(SITE + "/api/v4/vod/asset/details", {
                "detailsType": "all", "isDetailedView": True,
                "asset_ids": str(asset_id),
                "deviceDetails": SEARCH_BODY["deviceDetails"],
            })
            details = (rr.json()["assets"][0])["details"]
            tok = parse_token(details.get("hlsUrl") or "")
            if tok:
                log(f"  [v4] ok via asset {asset_id}")
                return tok, None
        except Exception:
            continue
    log("  [v4] no token from any live asset")
    return None


def strat_page():
    for path in ("/live-tv", "/"):
        try:
            r = _get(SITE + path, headers=WEB_HEADERS)
        except Exception:
            continue
        if r.status_code == 200:
            tok = parse_token(r.text)
            if tok:
                log(f"  [page] ok from {path}")
                return tok, None
    log("  [page] no token in HTML")
    return None


def strat_worker():
    for feed in WORKER_FEEDS:
        try:
            r = _get(feed, headers=WEB_HEADERS)
        except Exception as exc:
            log(f"  [worker] {exc}")
            continue
        if r.status_code != 200:
            log(f"  [worker] {feed} -> {r.status_code}")
            continue
        for line in r.text.splitlines():
            line = line.strip()
            if not line.startswith("http"):
                continue
            m = re.match(r"(https://[^/]+).*?\b(hdn(?:ea|tl))=([^&\s]+)", line)
            if m:
                log(f"  [worker] ok, host={m.group(1)}")
                return (m.group(2), m.group(3)), m.group(1)
    log("  [worker] no usable feed")
    return None


STRATEGIES = [("apiv2", strat_apiv2), ("web_v4", strat_web_v4),
              ("page", strat_page), ("worker", strat_worker)]


# --------------------------------------------------------------------------- #
# CDN + verification
# --------------------------------------------------------------------------- #
def verify(name: str, value: str, host: str, path: str) -> bool:
    try:
        r = _get(f"{host}/hls/live/{path}/master.m3u8?{name}={value}",
                 headers=WEB_HEADERS, cookie=f"{name}={value}")
        return r.status_code == 200 and "#EXTM3U" in r.text
    except Exception:
        return False


def _works(name: str, value: str) -> bool:
    probes = [CHANNELS[0], CHANNELS[len(CHANNELS) // 2]]
    return any(verify(name, value, h, c[4]) for h in CDN_HOSTS for c in probes)


def pick_cdn(name: str, value: str):
    probes = [CHANNELS[0], CHANNELS[len(CHANNELS) // 2], CHANNELS[-1]]
    for host in CDN_HOSTS:
        hits = sum(verify(name, value, host, c[4]) for c in probes)
        DEBUG.setdefault("cdn_probe", {})[host] = f"{hits}/{len(probes)}"
        if hits:
            log(f"  CDN ok: {host} ({hits}/{len(probes)})")
            return host
        log(f"  CDN dead: {host} (0/{len(probes)})")
    return None


def acquire() -> tuple[str, str, str] | None:
    for label, fn in STRATEGIES:
        try:
            res = fn()
        except Exception as exc:
            log(f"  [{label}] crashed: {exc}")
            continue
        if not res:
            continue
        (name, value), host = res
        host = host or pick_cdn(name, value)
        if host and _works(name, value):
            log(f"  ACCEPTED via {label}")
            DEBUG["strategy"] = label
            DEBUG["cdn"] = host
            return name, value, host
        log(f"  [{label}] token did not verify on CDN")
    return None


def health_check(name, value, host, workers=16):
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(lambda ch: (ch, verify(name, value, host, ch[4])),
                              CHANNELS))
    alive = [ch for ch, ok in results if ok]
    dead = [ch for ch, ok in results if not ok]
    DEBUG["health"] = {"alive": len(alive), "dead": [c[1] for c in dead]}
    for ch in dead:
        vlog(f"  dead: {ch[1]}")
    return alive


def fetch_live_events():
    try:
        r = _post(SITE + "/api/v4/vod/search", SEARCH_BODY)
        payload = r.json() or {}
    except Exception as exc:
        log(f"  live search skipped: {exc}")
        return []
    node = payload.get("0") or payload
    ids = [i["id"] for i in (node.get("assets") or []) if i.get("id")]
    out = []
    for asset_id in ids[:20]:
        try:
            rr = _post(SITE + "/api/v4/vod/asset/details", {
                "detailsType": "all", "isDetailedView": True,
                "asset_ids": str(asset_id),
                "deviceDetails": SEARCH_BODY["deviceDetails"],
            })
            d = rr.json()["assets"][0]["details"]
            url = re.sub(r"\s", "", d.get("hlsUrl") or "")
            if url.startswith("http"):
                out.append((f"{d.get('channel') or d.get('showname') or asset_id} [live]", url))
        except Exception:
            continue
    log(f"  live events: {len(out)}")
    return out


# --------------------------------------------------------------------------- #
# writer
# --------------------------------------------------------------------------- #
def build(channels, name, value, host, live, out):
    exp = token_expiry(value)
    lines = [
        "#EXTM3U",
        f"# Generated  : {datetime.now(IST):%d %b %Y, %H:%M:%S IST}",
        f"# Token exp  : {datetime.fromtimestamp(exp, IST):%d %b %Y, %H:%M:%S IST}"
        if exp else "# Token exp  : unknown",
        f"# CDN host   : {host}",
        f"# Source     : {DEBUG.get('strategy', '?')}",
        f"# Channels   : {len(channels)}" + (f" + {len(live)} live" if live else ""),
        "",
    ]
    for cid, title, cat, lang, path in channels:
        lines += [
            f'#EXTINF:-1 tvg-id="{cid}.sonyliv" tvg-name="{title}" '
            f'tvg-language="{lang}" group-title="{GROUP} - {cat}",{title}',
            f"#EXTVLCOPT:http-user-agent={UA}",
            f"#EXTVLCOPT:http-referrer={SITE}/",
            f"#EXTVLCOPT:http-cookie={name}={value}",
            "#EXTHTTP:" + json.dumps({
                "User-Agent": UA, "Referrer": SITE + "/",
                "Origin": SITE, "Cookie": f"{name}={value}",
            }, separators=(",", ":")),
            f"{host}/hls/live/{path}/master.m3u8?{name}={value}",
            "",
        ]
    for title, url in live:
        lines += [
            f'#EXTINF:-1 group-title="{GROUP} - Live Events",{title}',
            f"#EXTVLCOPT:http-user-agent={UA}",
            url,
            "",
        ]
    atomic_write(out, "\n".join(lines).rstrip() + "\n")


def atomic_write(path, text):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def dump_debug():
    try:
        DEBUG["when"] = datetime.now(IST).isoformat()
        with open(DEBUG_FILE, "w", encoding="utf-8") as fh:
            json.dump(DEBUG, fh, indent=2)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    global VERBOSE, DIAG

    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="sonyliv.m3u")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--prune", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--diag", action="store_true", help="print raw HTTP bodies")
    args = ap.parse_args()

    VERBOSE, DIAG = args.verbose, args.diag
    log(f"SonyLIV builder  py{sys.version.split()[0]}  cwd={os.getcwd()}")

    token = None
    if not args.force:
        m = re.search(r"hdn(?:ea|tl)=[^\s\"']+", open(args.out, encoding="utf-8").read()) \
            if os.path.exists(args.out) else None
        if m:
            tok = parse_token(m.group(0))
            left = token_expiry(tok[1]) - time.time() if tok else 0
            if left > REUSE_MARGIN:
                log(f"Reusing cached token ({left / 3600:.1f}h left)")
                token = tok
            else:
                log(f"Cached token {left / 3600:.1f}h left -> re-mint")

    if token is None:
        log("Acquiring token...")
        got = acquire()
        if got is None:
            dump_debug()
            log("FAILED: no strategy produced a working token.")
            return 1
        name, value, host = got
    else:
        name, value = token
        host = pick_cdn(name, value)
        if host is None:
            dump_debug()
            log("FAILED: cached token no longer valid on any CDN.")
            return 1

    DEBUG["token_name"] = name
    DEBUG["token_exp"] = token_expiry(value)

    if args.selftest:
        dump_debug()
        log(f"Selftest OK -- {datetime.fromtimestamp(token_expiry(value), IST):%d %b %H:%M} IST")
        return 0

    channels = CHANNELS
    if args.check or args.prune:
        log("Health-checking channels...")
        alive = health_check(name, value, host)
        if not alive:
            log(f"  WARNING 0/{len(CHANNELS)} healthy, writing full list")
            if args.strict:
                dump_debug()
                return 1
        elif args.prune:
            channels = alive
            log(f"  pruned to {len(alive)}/{len(CHANNELS)}")

    live = fetch_live_events() if args.live else []
    build(channels, name, value, host, live, args.out)
    dump_debug()
    log(f"OK -> {args.out}  entries={len(channels) + len(live)}  "
        f"valid_until={datetime.fromtimestamp(token_expiry(value), IST):%d %b %H:%M} IST")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
