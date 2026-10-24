#!/usr/bin/env python3
"""
SonyLIV -> auto-updating IPTV playlist (M3U). Free tier, no login, no premium.

WHY THIS WORKS
--------------
SonyLIV's CDN signs every request with an Akamai edge-auth token (hdnea/hdntl).
Channel paths are STATIC. The token is minted with `acl=/*`, so ONE token
covers every channel, every media playlist, every AES key and every segment.
No subscription check happens at mint time.

So the whole job is: mint a fresh token every few hours, template it into a
fixed channel map, atomically rewrite the .m3u. That is what "auto update" means.

TOKEN CHAIN (2 requests, no auth):
    GET  apiv2.sonyliv.com/AGL/1.4/A/ENG/WEB/ALL/GETTOKEN              -> JWT
    GET  apiv2.sonyliv.com/AGL/1.5/A/ENG/WEB/IN/CONTENT/VIDEOURL/VOD/{id}
         header: security_token: <JWT>                                 -> token

USAGE
    python scraper.py                      # write sonyliv.m3u
    python scraper.py --check              # verify before writing (use in CI)
    python scraper.py --prune              # drop channels that fail health check
    python scraper.py --live               # also append live sports/event feeds
    python scraper.py --force              # ignore cached token, always re-mint
    python scraper.py --selftest           # mint + verify + report, write nothing
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    sys.exit("Missing dependency ->  pip install requests")

# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
API_BASE   = "https://apiv2.sonyliv.com"
SITE       = "https://www.sonyliv.com"
TOKEN_PATH = "/AGL/1.4/A/ENG/WEB/ALL/GETTOKEN"
URL_PATH   = "/AGL/1.5/A/ENG/WEB/IN/CONTENT/VIDEOURL/VOD/{asset}"

# Primary CDN first; alternates are tried only if the primary stops verifying.
CDN_HOSTS = [
    "https://dishmt.slivcdn.com",
    "https://slivcdn.com",
    "https://sonylivmt.slivcdn.com",
]

# "Donor" VOD assets used purely to obtain a wildcard token. If one is pulled
# from the catalogue, the next is tried.
DONOR_ASSETS = ["1000022678", "1000027071", "1000026763", "1000024210"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

GROUP        = "SonyLIV"
REUSE_MARGIN = 3 * 3600          # reuse cached token while > 3h validity remains
HTTP_TIMEOUT = 20
IST          = timezone(timedelta(hours=5, minutes=30))

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": UA,
    "Referer":    SITE + "/",
    "Origin":     SITE,
    "Accept":     "*/*",
})

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


# --------------------------------------------------------------------------- #
# token
# --------------------------------------------------------------------------- #
def parse_token(raw: str) -> tuple[str, str] | None:
    """'hdnea=exp=...~hmac=...' -> ('hdnea', 'exp=...~hmac=...')"""
    m = TOKEN_RE.search(raw or "")
    return (m.group(1), m.group(2)) if m else None


def token_expiry(token: str) -> int:
    m = re.search(r"exp=(\d+)", token)
    return int(m.group(1)) if m else 0


def mint_token(retries: int = 3) -> tuple[str, str] | None:
    """Return (cookie_name, token_value) or None. Never raises."""
    for attempt in range(1, retries + 1):
        try:
            r = SESSION.get(API_BASE + TOKEN_PATH, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            jwt = (r.json() or {}).get("resultObj")
            if not isinstance(jwt, str) or not jwt:
                raise ValueError(f"GETTOKEN returned no JWT (HTTP {r.status_code})")

            asset = random.choice(DONOR_ASSETS)
            r2 = SESSION.get(
                API_BASE + URL_PATH.format(asset=asset),
                headers={"security_token": jwt},
                timeout=HTTP_TIMEOUT,
            )
            r2.raise_for_status()
            vurl = ((r2.json() or {}).get("resultObj") or {}).get("videoURL") or ""

            parsed = parse_token(vurl)
            if not parsed:
                raise ValueError(f"no edge-auth token in videoURL: {vurl[:110]!r}")
            return parsed

        except Exception as exc:
            print(f"  ! mint attempt {attempt}/{retries}: {exc}")
            if attempt < retries:
                time.sleep(2 * attempt)
    return None


def cached_token(path: str) -> tuple[str, str] | None:
    if not os.path.exists(path):
        return None
    try:
        return parse_token(open(path, encoding="utf-8").read())
    except OSError:
        return None


def verify(name: str, value: str, host: str, path: str) -> bool:
    """Confirm the token actually serves a master playlist."""
    url = f"{host}/hls/live/{path}/master.m3u8?{name}={value}"
    try:
        r = SESSION.get(url, timeout=HTTP_TIMEOUT, headers={"Cookie": f"{name}={value}"})
        return r.status_code == 200 and "#EXTM3U" in r.text
    except Exception:
        return False


def pick_cdn(name: str, value: str) -> str | None:
    """Return the first CDN host that serves the probe channel."""
    probe = CHANNELS[0][4]
    for host in CDN_HOSTS:
        if verify(name, value, host, probe):
            print(f"  CDN ok: {host}")
            return host
        print(f"  CDN dead: {host}")
    return None


# --------------------------------------------------------------------------- #
# health check / prune
# --------------------------------------------------------------------------- #
def health_check(name: str, value: str, host: str) -> list[tuple]:
    """Probe every channel in parallel-ish; return the ones that respond."""
    alive, dead = [], []
    for ch in CHANNELS:
        (alive if verify(name, value, host, ch[4]) else dead).append(ch)
    for ch in dead:
        print(f"  dead: {ch[1]}")
    return alive


# --------------------------------------------------------------------------- #
# live events (optional, best-effort)
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


def fetch_live_events() -> list[tuple[str, str]]:
    """Return [(display_name, m3u8_url)] for currently-live events. Best-effort."""
    try:
        r = SESSION.post(SITE + "/api/v4/vod/search", json=SEARCH_BODY,
                         headers={"Content-Type": "application/json"},
                         timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        payload = r.json()
    except Exception as exc:
        print(f"  ! live-event search failed: {exc}")
        return []

    assets = []
    node = payload.get("0") or payload
    for item in (node.get("assets") or []):
        if item.get("id"):
            assets.append(item["id"])

    out = []
    for asset_id in assets[:25]:
        try:
            body = {
                "detailsType": "all", "isDetailedView": True,
                "asset_ids": str(asset_id),
                "deviceDetails": SEARCH_BODY["deviceDetails"],
            }
            rr = SESSION.post(SITE + "/api/v4/vod/asset/details", json=body,
                              headers={"Content-Type": "application/json"},
                              timeout=HTTP_TIMEOUT)
            rr.raise_for_status()
            details = (rr.json()["assets"][0])["details"]
            url = re.sub(r"\s", "", details.get("hlsUrl") or "")
            title = details.get("channel") or details.get("showname") or str(asset_id)
            if url.startswith("http"):
                out.append((f"{title} [live]", url))
        except Exception:
            continue
    print(f"  live events: {len(out)}")
    return out


# --------------------------------------------------------------------------- #
# playlist writer
# --------------------------------------------------------------------------- #
def build(channels, name, value, host, live, out) -> None:
    exp = datetime.fromtimestamp(token_expiry(value), IST) if token_expiry(value) else None
    lines = [
        "#EXTM3U",
        f"# Generated  : {datetime.now(IST):%d %b %Y, %H:%M:%S IST}",
        f"# Token exp  : {exp:%d %b %Y, %H:%M:%S IST}" if exp else "# Token exp  : unknown",
        f"# CDN host   : {host}",
        f"# Channels   : {len(channels)}" + (f" + {len(live)} live events" if live else ""),
        f"# Token      : {name}=... (embedded in URL and mirrored as Cookie)",
        "",
    ]

    for cid, title, cat, lang, path in channels:
        url = f"{host}/hls/live/{path}/master.m3u8?{name}={value}"
        lines += [
            f'#EXTINF:-1 tvg-id="{cid}.sonyliv" tvg-name="{title}" '
            f'tvg-language="{lang}" group-title="{GROUP} - {cat}",{title}',
            f"#EXTVLCOPT:http-user-agent={UA}",
            f"#EXTVLCOPT:http-referrer={SITE}/",
            f"#EXTVLCOPT:http-cookie={name}={value}",
            '#EXTHTTP:' + json.dumps({
                "User-Agent": UA, "Referrer": SITE + "/",
                "Origin": SITE, "Cookie": f"{name}={value}",
            }, separators=(",", ":")),
            url,
            "",
        ]

    for title, url in live:
        lines += [
            f'#EXTINF:-1 group-title="{GROUP} - Live Events",{title}',
            f"#EXTVLCOPT:http-user-agent={UA}",
            f"#EXTVLCOPT:http-referrer={SITE}/",
            url,
            "",
        ]

    atomic_write(out, "\n".join(lines).rstrip() + "\n")


def atomic_write(path: str, text: str) -> None:
    """Write via temp file + os.replace so a crash never truncates the playlist."""
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


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="SonyLIV auto-updating M3U builder")
    ap.add_argument("-o", "--out", default="sonyliv.m3u")
    ap.add_argument("--force", action="store_true", help="ignore cached token")
    ap.add_argument("--check", action="store_true", help="verify before writing")
    ap.add_argument("--prune", action="store_true", help="drop channels that fail")
    ap.add_argument("--live", action="store_true", help="append live event feeds")
    ap.add_argument("--selftest", action="store_true", help="probe only, write nothing")
    args = ap.parse_args()

    print("SonyLIV playlist builder")

    # 1. token -- reuse a cached one while it is comfortably valid
    token = None
    if not args.force:
        token = cached_token(args.out)
        if token:
            left = token_expiry(token[1]) - time.time()
            if left > REUSE_MARGIN:
                print(f"Reusing cached token ({left / 3600:.1f}h left)")
            else:
                print(f"Cached token has {left / 3600:.1f}h left -> re-minting")
                token = None

    if token is None:
        print("Minting fresh token...")
        token = mint_token()
        if token is None:
            print("FAILED: no token. Existing playlist left untouched.")
            return 1
        print(f"  got {token[0]}={token[1][:40]}...")

    name, value = token

    # 2. CDN
    host = pick_cdn(name, value)
    if host is None:
        print("FAILED: token did not verify on any CDN host.")
        return 1

    if args.selftest:
        print(f"Selftest OK -- token valid until "
              f"{datetime.fromtimestamp(token_expiry(value), IST):%d %b %H:%M} IST")
        return 0

    # 3. optional verification / pruning
    channels = CHANNELS
    if args.check or args.prune:
        print("Health-checking channels...")
        channels = health_check(name, value, host)
        if not channels:
            print("FAILED: zero channels alive.")
            return 1
        if args.prune:
            print(f"  kept {len(channels)}/{len(CHANNELS)}")
        else:
            channels = CHANNELS

    # 4. optional live events
    live = fetch_live_events() if args.live else []

    # 5. write
    build(channels, name, value, host, live, args.out)
    exp = datetime.fromtimestamp(token_expiry(value), IST)
    total = len(channels) + len(live)
    print(f"Wrote {args.out} -- {total} entries, token valid until {exp:%d %b %H:%M} IST")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
