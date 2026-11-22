"""Signed-in accounts for Naver Webtoon and WEBTOON (webtoons.com).

The user signs in on the site's own login page in a separate window (login_window.py);
this app never sees the password. Only the resulting login cookies for that site are kept,
in a file only this Mac user can read. Signing out deletes them.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STORE_DIR = os.path.expanduser("~/Library/Application Support/webtoon-dl")
STORE = os.path.join(STORE_DIR, "accounts.json")

SITES = {
    "naver": {
        "label": "Naver Webtoon",
        "login_url": "https://nid.naver.com/nidlogin.login?mode=form&url=https%3A%2F%2Fcomic.naver.com%2F",
        "home": "https://comic.naver.com/",
        "domain": "naver.com",
        "session_cookies": ("NID_AUT", "NID_SES"),
    },
    "webtoons": {
        "label": "WEBTOON (webtoons.com)",
        "login_url": "https://www.webtoons.com/member/login?returnUrl=https%3A%2F%2Fwww.webtoons.com%2Fen%2F",
        "home": "https://www.webtoons.com/",
        "domain": "webtoons.com",
        "session_cookies": ("NEO_SES", "NEO_CHK", "neo_ses"),
    },
}

_lock = threading.Lock()
_pending: dict[str, subprocess.Popen] = {}


def _load() -> dict:
    try:
        with open(STORE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    os.makedirs(STORE_DIR, exist_ok=True)
    tmp = STORE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, STORE)
    os.chmod(STORE, 0o600)


def _alive(c: dict) -> bool:
    exp = c.get("expires")
    return not exp or exp > time.time()


def cookie_header(host: str) -> str | None:
    """Cookie header for a request to `host`, if the user is signed in to that site."""
    host = host.lower()
    data = _load()
    for site, cfg in SITES.items():
        if not (host == cfg["domain"] or host.endswith("." + cfg["domain"])):
            continue
        cookies = [c for c in (data.get(site) or {}).get("cookies", []) if _alive(c)]
        if not cookies:
            return None
        out = []
        for c in cookies:
            dom = c.get("domain", "").lstrip(".").lower()
            if not dom or host == dom or host.endswith("." + dom):
                out.append(f'{c["name"]}={c["value"]}')
        return "; ".join(out) or None
    return None


def signed_in(site: str) -> bool:
    cfg = SITES[site]
    cookies = (_load().get(site) or {}).get("cookies", [])
    names = {c["name"] for c in cookies if _alive(c)}
    return any(n in names for n in cfg["session_cookies"])


def status() -> dict:
    data = _load()
    out = {}
    for site, cfg in SITES.items():
        with _lock:
            p = _pending.get(site)
            waiting = bool(p and p.poll() is None)
        out[site] = {
            "label": cfg["label"],
            "signedIn": signed_in(site),
            "since": (data.get(site) or {}).get("saved", 0),
            "waiting": waiting,
        }
    return out


def sign_out(site: str) -> None:
    data = _load()
    if site in data:
        del data[site]
        _save(data)


def start_sign_in(site: str) -> None:
    """Open the login window in its own process; store the cookies when it finishes."""
    if site not in SITES:
        raise ValueError("unknown site")
    with _lock:
        p = _pending.get(site)
        if p and p.poll() is None:
            return  # already open
        cfg = SITES[site]
        args = [sys.executable, os.path.join(HERE, "login_window.py"), cfg["login_url"], cfg["home"],
                cfg["domain"], ",".join(cfg["session_cookies"]), cfg["label"]]
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        _pending[site] = proc

    def collect():
        out, _ = proc.communicate()
        try:
            cookies = json.loads(out.strip().splitlines()[-1]) if out.strip() else []
        except ValueError:
            cookies = []
        if cookies:
            data = _load()
            data[site] = {"cookies": cookies, "saved": int(time.time())}
            _save(data)

    threading.Thread(target=collect, daemon=True).start()
