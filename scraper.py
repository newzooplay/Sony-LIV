#!/usr/bin/env python3
"""
SonyLIV -> auto-updating IPTV playlist (M3U). Free tier, no login, no premium.

Design rule: NEVER fail the job while a usable playlist can still be produced.
Exit 0 whenever something was written. Exit 1 only if nothing could be.

USAGE
    python scraper.py                          # write sonyliv.m3u
    python scraper.py --check --prune --live   # full CI run
    python scraper.py --selftest               # probe only, writes nothing
    python scraper.py --verbose                # per-channel diagnostics
    python scraper.py --strict                 # hard-fail on zero healthy channels
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
URL_PATH   = "/AGL/1.5/A/ENG/WEB/IN/CONTENT/VIDEOURL/VOD/{asset}"

CDN_HOSTS = [
    "https://dishmt.slivcdn.com",
    "https://slivcdn.com",
    "https://sonylivmt.slivcdn.com",
]

DONOR_ASSETS = ["1000022678", "1000027071", "1000026763", "1000024210"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

BASE_HEADERS = {
    "User-Agent": UA,
    "Referer":    SITE + "/",
    "Origin":     SITE,
    "Accept":     "*/*",
}

GROUP        = "SonyLIV"
REUSE_MARGIN = 3 * 3600
HTTP_TIMEOUT = 12          # keep short: probes run 16-wide
IST          = timezone(timedelta(hours=5, minutes=30))
DEBUG_FILE   = "sonyliv.debug.json"

VERBOSE = False

# --------------------------------------------------------------------------- #
# channel map  ->  (tvg-id, name, category, language, cdn path)
# --------------------------------------------------------------------------- #
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

TOKEN_RE = re.compile(r"\b(hdn(?:ea|tl))=([^&\s\"']+)")
DEBUG: dict = {}


def log(msg: str) -> None:
    print(msg, flush=True)


def vlog(msg: str) -> None:
    if VERBOSE:
        print(msg, flush=True)


# --------------------------------------------------------------------------- #
# low-level http (thread-safe: no shared Session)
# --------------------------------------------------------------------------- #
def _get(url: str, cookie: str | None = None, timeout: int = HTTP_TIMEOUT):
    headers = dict(BASE_HEADERS)
    if cookie:
        headers["Cookie"] = cookie
    return requests.get(url, headers=headers, timeout=timeout)


def _post(url: str, payload: dict, timeout: int = HTTP_TIMEOUT):
    headers = dict(BASE_HEADERS)
    headers["Content-Type"] = "application/json"
    return requests.post(url, json=payload, headers=headers, timeout=timeout)


# --------------------------------------------------------------------------- #
# token
# --------------------------------------------------------------------------- #
def parse_token(raw: str):
    m = TOKEN_RE.search(raw or "")
    return (m.group(1), m.group(2)) if m else None


def token_expiry(value: str) -> int:
    m = re.search(r"exp=(\d+)", value)
    return int(m.group(1)) if m else 0


def mint_token(retries: int = 3):
    for attempt in range(1, retries + 1):
        try:
            r = _get(API_BASE + TOKEN_PATH)
            r.raise_for_status()
            jwt = (r.json() or {}).get("resultObj")
            if not isinstance(jwt, str) or not jwt:
                raise ValueError(f"GETTOKEN gave no JWT (HTTP {r.status_code})")

            asset = random.choice(DONOR_ASSETS)
            r2 = requests.get(
                API_BASE + URL_PATH.format(asset=asset),
                headers={**BASE_HEADERS, "security_token": jwt},
                timeout=HTTP_TIMEOUT,
            )
            r2.raise_for_status()
            vurl = ((r2.json() or {}).get("resultObj") or {}).get("videoURL") or ""

            parsed = parse_token(vurl)
            if not parsed:
                raise ValueError(f"no edge-auth token in videoURL: {vurl[:110]!r}")
            DEBUG["donor_asset"] = asset
            return parsed
        except Exception as exc:
            log(f"  ! mint {attempt}/{retries}: {exc}")
            if attempt < retries:
                time.sleep(2 * attempt)
    return None


def cached_token(path: str):
    if not os.path.exists(path):
        return None
    try:
        return parse_token(open(path, encoding="utf-8").read())
    except OSError:
        return None


def verify(name: str, value: str, host: str, path: str) -> bool:
    try:
        r = _get(f"{host}/hls/live/{path}/master.m3u8?{name}={value}",
                 cookie=f"{name}={value}")
        return r.status_code == 200 and "#EXTM3U" in r.text
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# CDN + channel health (threaded)
# --------------------------------------------------------------------------- #
def pick_cdn(name: str, value: str):
    """A host is good if ANY of 3 spread-out probe channels answers."""
    probes = [CHANNELS[0], CHANNELS[len(CHANNELS) // 2], CHANNELS[-1]]
    for host in CDN_HOSTS:
        hits = sum(verify(name, value, host, c[4]) for c in probes)
        DEBUG.setdefault("cdn_probe", {})[host] = f"{hits}/{len(probes)}"
        if hits:
            log(f"  CDN ok: {host} ({hits}/{len(probes)} probes)")
            return host
        log(f"  CDN dead: {host} (0/{len(probes)} probes)")
    return None


def health_check(name: str, value: str, host: str, workers: int = 16):
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(
            lambda ch: (ch, verify(name, value, host, ch[4])), CHANNELS))
    alive = [ch for ch, ok in results if ok]
    dead = [ch for ch, ok in results if not ok]
    DEBUG["health"] = {"alive": len(alive), "dead": [c[1] for c in dead]}
    for ch in dead:
        vlog(f"  dead: {ch[1]}")
    return alive


# --------------------------------------------------------------------------- #
# live events (best effort)
# --------------------------------------------------------------------------- #
SEARCH_BODY = {
    "detailsType": "basic",
    "searchSet": [{
        "pageSize": 60, "pageNumber": 0, "sortOrder": "START_DATE:DESC",
        "data": "all=classification:channel&all=type:live",
        "type": "search", "id": "live_channels", "itemsUsed": 0, "language": "",
    }],
    "deviceDetails": {"mfg": "WEB", "os": "others", "osVer": "XXX",
                      "model": "WEB", "deviceId": "a1b2c3d4-0001",
                      "platform": "web"},
    "isSearchable": True,
}


def fetch_live_events():
    try:
        r = _post(SITE + "/api/v4/vod/search", SEARCH_BODY)
        r.raise_for_status()
        payload = r.json() or {}
    except Exception as exc:
        log(f"  ! live search skipped: {exc}")
        return []

    node = payload.get("0") or payload
    ids = [i["id"] for i in (node.get("assets") or []) if i.get("id")]

    out = []
    for asset_id in ids[:25]:
        try:
            rr = _post(SITE + "/api/v4/vod/asset/details", {
                "detailsType": "all", "isDetailedView": True,
                "asset_ids": str(asset_id),
                "deviceDetails": SEARCH_BODY["deviceDetails"],
            })
            rr.raise_for_status()
            details = rr.json()["assets"][0]["details"]
            url = re.sub(r"\s", "", details.get("hlsUrl") or "")
            title = details.get("channel") or details.get("showname") or str(asset_id)
            if url.startswith("http"):
                out.append((f"{title} [live]", url))
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


def atomic_write(path: str, text: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def dump_debug() -> None:
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
    global VERBOSE

    ap = argparse.ArgumentParser(description="SonyLIV auto-updating M3U builder")
    ap.add_argument("-o", "--out", default="sonyliv.m3u")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--prune", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    VERBOSE = args.verbose
    log(f"SonyLIV builder  python {sys.version.split()[0]}  cwd={os.getcwd()}")

    # ---- token ------------------------------------------------------------
    token = None
    if not args.force:
        token = cached_token(args.out)
        if token:
            left = token_expiry(token[1]) - time.time()
            if left > REUSE_MARGIN:
                log(f"Reusing cached token ({left / 3600:.1f}h left)")
            else:
                log(f"Cached token {left / 3600:.1f}h left -> re-mint")
                token = None

    if token is None:
        log("Minting fresh token...")
        token = mint_token()
        if token is None:
            dump_debug()
            log("FAILED: no token. Existing playlist untouched.")
            return 1
        log(f"  got {token[0]}={token[1][:40]}...")

    name, value = token
    DEBUG["token_name"] = name
    DEBUG["token_exp"] = token_expiry(value)

    # ---- CDN --------------------------------------------------------------
    host = pick_cdn(name, value)
    if host is None:
        dump_debug()
        log("FAILED: token verified on no CDN host.")
        return 1

    if args.selftest:
        dump_debug()
        log(f"Selftest OK -- token valid until "
            f"{datetime.fromtimestamp(token_expiry(value), IST):%d %b %H:%M} IST")
        return 0

    # ---- health (optional, threaded, non-fatal) ---------------------------
    channels = CHANNELS
    if args.check or args.prune:
        log("Health-checking channels (threaded)...")
        alive = health_check(name, value, host)
        if not alive:
            log(f"  WARNING: 0/{len(CHANNELS)} healthy. Writing full list anyway.")
            if args.strict:
                dump_debug()
                return 1
        elif args.prune:
            channels = alive
            log(f"  pruned to {len(alive)}/{len(CHANNELS)}")
        else:
            log(f"  {len(alive)}/{len(CHANNELS)} healthy (no prune)")

    # ---- live -------------------------------------------------------------
    live = fetch_live_events() if args.live else []

    # ---- write ------------------------------------------------------------
    build(channels, name, value, host, live, args.out)
    dump_debug()
    exp = datetime.fromtimestamp(token_expiry(value), IST)
    log(f"OK -> {args.out}  entries={len(channels) + len(live)}  "
        f"token_valid_until={exp:%d %b %H:%M} IST")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
