#!/usr/bin/env python3
"""
SonyLIV auto-updating M3U builder -- no login, no premium, no third-party feed.

WHY IT WORKS
------------
SonyLIV's CDN (dishmt.slivcdn.com) signs every request with an Akamai `hdnea`
token. The channel paths are STATIC (e.g. /hls/live/2011671/SETHD/master.m3u8).
Only the token rotates, and the token is minted with `acl=/*` -- meaning ONE
token covers every channel, every media playlist, every AES key and every
segment. No subscription check happens at mint time.

So the whole job is: mint a fresh token every few hours, template it into a
fixed channel map, write the .m3u. That is what "auto update" actually means.

TOKEN CHAIN (2 requests, no auth):
    GET  apiv2.sonyliv.com/AGL/1.4/A/ENG/WEB/ALL/GETTOKEN          -> JWT
    GET  apiv2.sonyliv.com/AGL/1.5/A/ENG/WEB/IN/CONTENT/VIDEOURL/VOD/1000022678
         header: security_token: <JWT>                             -> hdnea token

Usage:
    python scraper.py                    # write sonyliv.m3u
    python scraper.py --check            # verify the token really plays
    python scraper.py -o out.m3u --force # ignore cached token
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# ------------------------------------------------------------------ config
API_BASE = "https://apiv2.sonyliv.com"
CDN_BASE = "https://dishmt.slivcdn.com/hls/live"
SITE     = "https://www.sonyliv.com"
TOKEN_PATH = "/AGL/1.4/A/ENG/WEB/ALL/GETTOKEN"
URL_PATH   = "/AGL/1.5/A/ENG/WEB/IN/CONTENT/VIDEOURL/VOD/1000022678"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

GROUP      = "SonyLIV"
REUSE_MARGIN = 3 * 3600        # keep a cached token while it still has > 3h
IST = timezone(timedelta(hours=5, minutes=30))

S = requests.Session()
S.headers.update({
    "User-Agent": UA,
    "Referer": SITE + "/",
    "Origin": SITE,
    "Accept": "*/*",
})

# --------------------------------------------------------------- channels
# (tvg-id, display name, category, language, cdn path)
CHANNELS = [
    ("sony-hd",        "SONY HD",         "Entertainment", "Hindi",   "2011671/SETHD"),
    ("sab-hd",         "SAB HD",          "Entertainment", "Hindi",   "2011749/SABHD"),
    ("sony-marathi",   "SONY MARATHI",    "Entertainment", "Marathi", "2011740/SonyMarathi"),
    ("sony-pal",       "SONY PAL",        "Entertainment", "Hindi",   "2011741/SonyPalSD"),
    ("sony-aath",      "SONY AATH",       "Entertainment", "Bengali", "2011641/SonyAathSD"),
    ("sony-yay",       "SONY YAY",        "Kids",          "Hindi",   "2011746/SonyYaySD"),
    ("sony-max-hd",    "SONY MAX HD",     "Movies",        "Hindi",   "2011670/SonyMaxhd"),
    ("sony-max",       "SONY MAX",        "Movies",        "Hindi",   "2011745/SonyMaxSD"),
    ("sony-max2",      "SONY MAX2",       "Movies",        "Hindi",   "2011908/MAX2"),
    ("sony-wah",       "SONY WAH",        "Movies",        "Hindi",   "2011906/SonyWah"),
    ("sony-pix-hd",    "SONY PIX HD",     "Movies",        "Hindi",   "2011748/PIXHD"),
    ("sony-ten-1-hd",  "SONY TEN 1 HD",   "Sports",        "English", "2011747/TEN1HD"),
    ("sony-ten-1",     "SONY TEN 1",      "Sports",        "English", "2011739/TEN1SD"),
    ("sony-ten-2-hd",  "SONY TEN 2 HD",   "Sports",        "English", "2020434/TEN2HD"),
    ("sony-ten-2",     "SONY TEN 2",      "Sports",        "English", "2020590/TEN2SD"),
    ("sony-ten-3-hd",  "SONY TEN 3 HD",   "Sports",        "Hindi",   "2020591/TEN3HD"),
    ("sony-ten-3",     "SONY TEN 3",      "Sports",        "Hindi",   "2020592/TEN3SD"),
    ("sony-ten-4-hd",  "SONY TEN 4 HD",   "Sports",        "Telugu",  "2020589/ten4hd"),
    ("sony-ten-4",     "SONY TEN 4",      "Sports",        "Telugu",  "2020437/ten4sd"),
    ("sony-ten-5-hd",  "SONY TEN 5 HD",   "Sports",        "Hindi",   "2020593/SONYSIXHD"),
    ("sony-ten-5",     "SONY TEN 5",      "Sports",        "English", "2020594/SONYSIXSD"),
    ("bbc-earth",      "BBC EARTH",       "Infotainment",  "English", "2011907/SonyBBCEarthHD"),
]


# ------------------------------------------------------------------ token
def mint_token(retries: int = 3) -> str | None:
    """Return a fresh wildcard `hdnea=...` token, or None."""
    for attempt in range(1, retries + 1):
        try:
            r = S.get(API_BASE + TOKEN_PATH, timeout=20)
            jwt = r.json().get("resultObj")
            if not isinstance(jwt, str) or not jwt:
                raise ValueError(f"GETTOKEN gave no JWT (HTTP {r.status_code})")

            r2 = S.get(API_BASE + URL_PATH,
                       headers={"security_token": jwt}, timeout=20)
            vurl = (r2.json().get("resultObj") or {}).get("videoURL") or ""
            m = re.search(r"hdnea=([^&\s]+)", vurl)
            if not m:
                raise ValueError(f"no hdnea in videoURL: {vurl[:120]!r}")
            return "hdnea=" + m.group(1)
        except Exception as e:
            print(f"  mint attempt {attempt}/{retries} failed: {e}")
            time.sleep(2 * attempt)
    return None


def token_expiry(token: str) -> int | None:
    m = re.search(r"exp=(\d+)", token)
    return int(m.group(1)) if m else None


def cached_token(path: str) -> str | None:
    """Read the token already inside an existing playlist."""
    if not os.path.exists(path):
        return None
    m = re.search(r"hdnea=[^\s\"&]+", open(path, encoding="utf-8").read())
    return m.group(0) if m else None


def verify(token: str, path: str = CHANNELS[0][4]) -> bool:
    """Confirm the token really serves a master playlist."""
    url = f"{CDN_BASE}/{path}/master.m3u8?{token}"
    try:
        r = S.get(url, timeout=20, headers={"Cookie": token})
        ok = r.status_code == 200 and "#EXTM3U" in r.text
        print(f"  verify: HTTP {r.status_code} {'OK' if ok else 'FAIL'}")
        return ok
    except Exception as e:
        print(f"  verify error: {e}")
        return False


# ------------------------------------------------------------------ build
def build(token: str, out: str) -> int:
    lines = [
        "#EXTM3U",
        f"# Generated  : {datetime.now(IST).strftime('%d %b %Y, %H:%M:%S IST')}",
        f"# Token exp  : {datetime.fromtimestamp(token_expiry(token) or 0, IST):%d %b %Y, %H:%M:%S IST}",
        "# CDN host   : dishmt.slivcdn.com",
        f"# Channels   : {len(CHANNELS)}",
        "# Token is embedded in each URL and mirrored as a Cookie header.",
        "",
    ]
    for cid, name, cat, lang, path in CHANNELS:
        url = f"{CDN_BASE}/{path}/master.m3u8?{token}"
        lines += [
            f'#EXTINF:-1 tvg-id="{cid}.sonyliv" tvg-name="{name}" '
            f'tvg-logo="" tvg-language="{lang}" group-title="{GROUP} - {cat}",{name}',
            f"#EXTVLCOPT:http-user-agent={UA}",
            f"#EXTVLCOPT:http-referrer={SITE}/",
            f"#EXTVLCOPT:http-cookie={token}",
            "#EXTHTTP:" + '{"User-Agent":"%s","Referrer":"%s/","Origin":"%s","Cookie":"%s"}'
            % (UA, SITE, SITE, token),
            url,
            "",
        ]
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    open(out, "w", encoding="utf-8").write("\n".join(lines).rstrip() + "\n")
    return len(CHANNELS)


# ------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="SonyLIV auto-updating M3U builder")
    ap.add_argument("-o", "--out", default="sonyliv.m3u")
    ap.add_argument("--force", action="store_true", help="ignore cached token")
    ap.add_argument("--check", action="store_true", help="verify token before writing")
    a = ap.parse_args()

    token = None
    if not a.force:
        token = cached_token(a.out)
        if token:
            left = (token_expiry(token) or 0) - time.time()
            if left > REUSE_MARGIN:
                print(f"Reusing cached token ({left/3600:.1f}h left) -- no mint needed")
            else:
                print(f"Cached token has {left/3600:.1f}h left -> re-minting")
                token = None

    if not token:
        print("Minting fresh token...")
        token = mint_token()
        if not token:
            print("FAILED: could not mint token")
            return 1
        print(f"  got: {token[:48]}...")

    if a.check and not verify(token):
        return 1

    n = build(token, a.out)
    exp = datetime.fromtimestamp(token_expiry(token) or 0, IST)
    print(f"Wrote {a.out} -- {n} channels, token valid until {exp:%d %b %H:%M} IST")
    return 0


if __name__ == "__main__":
    sys.exit(main())
