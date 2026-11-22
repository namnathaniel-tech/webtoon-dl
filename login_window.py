"""Shows a site's real login page in a window and prints that site's cookies as JSON.

Run by accounts.start_sign_in(); not meant to be used directly.
argv: login_url home_url domain session_cookie_names label

The user types their password into the site itself; this script only reads the cookies
for `domain` once the session cookie appears (or when the window is closed).
"""
from __future__ import annotations

import json
import sys
import threading
import time

import webview

login_url, home, domain, session_names, label = sys.argv[1:6]
session_names = set(session_names.split(","))
result: list[dict] = []
done = threading.Event()


def read_cookies(win) -> list[dict]:
    out = []
    for jar in win.get_cookies() or []:
        for name, m in jar.items():
            dom = (m["domain"] or "").lower()
            if not dom.lstrip(".").endswith(domain):
                continue
            raw, exp = m["expires"], None
            if hasattr(raw, "timeIntervalSince1970"):      # NSDate from WKWebView
                exp = float(raw.timeIntervalSince1970())
            elif isinstance(raw, (int, float)):
                exp = float(raw)
            elif isinstance(raw, str) and raw:
                try:
                    exp = time.mktime(time.strptime(raw, "%a, %d %b %Y %H:%M:%S %Z"))
                except ValueError:
                    exp = None
            out.append({"name": name, "value": m.value, "domain": dom, "path": m["path"] or "/",
                        "expires": exp})
    return out


def watch(win):
    # Wait until the login page has sent the user back to the site with a session cookie.
    while not done.is_set():
        time.sleep(1.5)
        try:
            url = win.get_current_url() or ""
            if domain in url and "login" not in url:
                cookies = read_cookies(win)
                if session_names & {c["name"] for c in cookies}:
                    result[:] = cookies
                    done.set()
                    win.destroy()
                    return
        except Exception:
            pass


def on_closing():
    if not result:
        try:
            cookies = read_cookies(window)
            if session_names & {c["name"] for c in cookies}:
                result[:] = cookies
        except Exception:
            pass
    done.set()


window = webview.create_window(f"Sign in to {label}", login_url, width=480, height=760)
window.events.closing += on_closing
webview.start(watch, window, private_mode=True)
print(json.dumps(result))
