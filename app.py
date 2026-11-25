#!/usr/bin/env python3
"""Local web UI for webtoon_dl. Run: python3 app.py  then open http://127.0.0.1:8765"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import accounts
import library
import webtoon_dl as wd

HOST, PORT = "127.0.0.1", int(os.environ.get("WEBTOON_PORT", 8765))
HERE = os.path.dirname(os.path.abspath(__file__))

_series_cache: dict[str, wd.Series] = {}
_jobs: dict[str, dict] = {}


class Cancelled(Exception):
    pass
_lock = threading.Lock()


def run_job(job_id: str, s: wd.Series, nos: list[int], fmt: str, out: str, translate: bool) -> None:
    job = _jobs[job_id]
    eps = [e for e in s.episodes if e.no in set(nos)]
    for ep in eps:
        if job["cancel"]:
            job["status"] = "cancelled"
            return
        item = {"no": ep.no, "title": ep.title, "state": "downloading", "images": 0, "path": "", "error": "",
                "translate": None}
        with _lock:
            job["items"].append(item)
            job["current"] = ep.no

        # these are called many times during an episode, so Cancel stops it mid-way
        def bump(item=item):
            if job["cancel"]:
                raise Cancelled()
            with _lock:
                item["images"] += 1
        def tr_progress(i, n, item=item):
            if job["cancel"]:
                raise Cancelled()
            with _lock:
                item["state"] = "translating"
                item["translate"] = [i, n]
        try:
            item["path"] = wd.download_episode(s, ep, out, fmt, on_image=bump,
                                               translate=translate, on_translate=tr_progress)
            item["state"] = "done"
        except Cancelled:
            with _lock:
                item["state"] = "failed"
                item["error"] = "cancelled"
                job["status"] = "cancelled"
                job["current"] = None
            return
        except Exception as e:
            item["state"] = "failed"
            item["error"] = str(e)
        with _lock:
            job["done"] += 1
    job["status"] = "finished"
    job["current"] = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _library(self):
        """/api/library?root=…                      → series list
           /api/library/pages?root=…&s=…&e=…         → pages with sizes
           /lib?root=…&s=…&e=…&f=…                   → one page image"""
        path, _, qs = self.path.partition("?")
        q = {k: v[0] for k, v in urllib.parse.parse_qs(qs).items()}
        root = os.path.expanduser(q.get("root") or wd.DEFAULT_OUT)
        try:
            if path == "/api/library":
                return self._send(200, {"root": root, "series": library.list_library(root)})
            if path == "/api/library/covers":   # fetch missing cover art and AniList info
                added = library.backfill_anilist(root) + library.backfill_covers(root)
                return self._send(200, {"added": added})
            if path == "/libcover":
                data = library.read_cover(root, q["s"])
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                return self.wfile.write(data)
            if path == "/api/library/pages":
                return self._send(200, {"pages": library.pages(root, q["s"], q["e"])})
            if path == "/lib":
                data, ctype = library.read_page(root, q["s"], q["e"], q["f"])
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=3600")
                self.end_headers()
                return self.wfile.write(data)
        except (KeyError, FileNotFoundError):
            return self._send(404, {"error": "not found"})
        except BrokenPipeError:
            return
        self._send(404, {"error": "not found"})

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if self.path.startswith("/icons/"):
            name = os.path.basename(self.path.split("?", 1)[0])
            path = os.path.join(HERE, "icons", name)
            if name.endswith(".png") and os.path.isfile(path):
                with open(path, "rb") as f:
                    return self._send(200, f.read(), "image/png")
            return self._send(404, {"error": "no icon"})
        if self.path.startswith(("/api/library", "/lib?", "/libcover?")):
            return self._library()
        if self.path == "/api/keys":
            try:
                import translate
                return self._send(200, translate.key_status())
            except ImportError as e:
                return self._send(500, {"error": str(e)})
        if self.path == "/api/accounts":
            return self._send(200, accounts.status())
        if self.path == "/api/config":
            return self._send(200, {"outDir": wd.DEFAULT_OUT, "pdf": _has_pil(), "translate": _can_translate(),
                                    "provider": _provider()})
        if self.path.startswith("/api/jobs/"):
            job = _jobs.get(self.path.rsplit("/", 1)[1])
            if not job:
                return self._send(404, {"error": "no such job"})
            with _lock:
                return self._send(200, {k: v for k, v in job.items() if k != "cancel"})
        if self.path.startswith("/api/thumb?"):
            # proxy thumbnails (pstatic blocks hotlinking without a referer)
            from urllib.parse import parse_qs
            u = parse_qs(self.path.split("?", 1)[1]).get("u", [""])[0]
            host = urllib.parse.urlparse(u).netloc
            if not (host.endswith(".pstatic.net") or host == "uploads.mangadex.org"):
                return self._send(400, {"error": "bad thumb"})
            try:
                ref = ("https://www.webtoons.com/" if "webtoon-phinf" in u
                       else "https://m.blog.naver.com/" if "blogthumb" in u or "blogfiles" in u
                       else "https://comic.naver.com/")
                return self._send(200, wd.fetch(u, ref), "image/jpeg")
            except Exception:
                return self._send(404, {"error": "thumb failed"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            body = self._body()
        except Exception:
            return self._send(400, {"error": "bad json"})
        if self.path == "/api/keys":
            origin = self.headers.get("Origin", "")
            if origin and origin not in (f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"):
                return self._send(403, {"error": "forbidden"})
            try:
                import translate
                svc, key = body.get("service"), str(body.get("key", ""))
                if svc == "deepl":
                    translate.save_deepl_key(key)
                elif svc == "groq":
                    translate.save_groq_key(key)
                else:
                    return self._send(400, {"error": "unknown service"})
                return self._send(200, translate.key_status())
            except Exception as e:
                return self._send(400, {"error": str(e)})
        if self.path == "/api/deepl-key":
            origin = self.headers.get("Origin", "")
            if origin and origin not in (f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"):
                return self._send(403, {"error": "forbidden"})
            try:
                import translate
                translate.save_deepl_key(str(body.get("key", "")))
            except Exception as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"translate": _can_translate(), "provider": _provider()})
        if self.path in ("/api/accounts/signin", "/api/accounts/signout"):
            # only this app's own page may sign in/out (not some other website in the browser)
            origin = self.headers.get("Origin", "")
            if origin and origin not in (f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"):
                return self._send(403, {"error": "forbidden"})
            site = body.get("site", "")
            if site not in accounts.SITES:
                return self._send(400, {"error": "unknown site"})
            if self.path.endswith("signin"):
                try:
                    accounts.start_sign_in(site)
                except Exception as e:
                    return self._send(500, {"error": f"couldn't open the sign-in window: {e}"})
            else:
                accounts.sign_out(site)
                _series_cache.clear()
            return self._send(200, accounts.status())
        if self.path == "/api/series":
            try:
                s = wd.get_series(body.get("url", ""), body.get("lang") or None)
            except Exception as e:
                return self._send(400, {"error": str(e)})
            key = f"{s.site}:{s.title_id}:{s.lang}"
            _series_cache[key] = s
            return self._send(200, {"key": key, **s.to_dict()})
        if self.path == "/api/download":
            s = _series_cache.get(body.get("key", ""))
            if not s:
                return self._send(400, {"error": "load the series first"})
            fmt = body.get("format", "images")
            if fmt not in wd.FORMATS:
                return self._send(400, {"error": "bad format"})
            out = os.path.expanduser(body.get("outDir") or wd.DEFAULT_OUT)
            nos = [int(n) for n in body.get("episodes", [])]
            if not nos:
                return self._send(400, {"error": "pick at least one episode"})
            job_id = uuid.uuid4().hex[:10]
            _jobs[job_id] = {"id": job_id, "title": s.title, "total": len(nos), "done": 0,
                             "status": "running", "current": None, "items": [],
                             "outDir": os.path.join(out, wd.safe_name(s.title)), "cancel": False}
            translate = bool(body.get("translate"))
            if translate and not _can_translate():
                return self._send(400, {"error": "Translation needs a DeepL key (DEEPL_API_KEY or ~/Library/Application Support/webtoon-dl/deepl.key), a Groq key, or ANTHROPIC_API_KEY."})
            threading.Thread(target=run_job, args=(job_id, s, nos, fmt, out, translate), daemon=True).start()
            return self._send(200, {"jobId": job_id})
        if self.path == "/api/cancel":
            job = _jobs.get(body.get("jobId", ""))
            if job:
                job["cancel"] = True
            return self._send(200, {"ok": True})
        if self.path == "/api/reveal":
            path = body.get("path", "")
            known = any(path == j["outDir"] or path.startswith(j["outDir"] + os.sep) for j in _jobs.values())
            if known and os.path.exists(path) and sys.platform == "darwin":
                subprocess.run(["open", "-R", path] if os.path.isfile(path) else ["open", path])
                return self._send(200, {"ok": True})
            return self._send(400, {"error": "can't open that"})
        self._send(404, {"error": "not found"})


def _can_translate() -> bool:
    try:
        import translate
        import anthropic  # noqa: F401
        return translate.available()
    except ImportError:
        return False


def _provider() -> str:
    try:
        import translate
        return translate.provider()
    except ImportError:
        return ""


def _has_pil() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


def main():
    url = f"http://{HOST}:{PORT}"
    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        if e.errno != 48 and "in use" not in str(e):
            raise
        print(f"Port {PORT} is already in use — the app is probably already running. Opening {url}")
        print(f"(To use another port: WEBTOON_PORT=8766 {sys.argv[0]})")
        if "--no-browser" not in sys.argv:
            webbrowser.open(url)
        return
    print(f"Webtoon Downloader running at {url}  (Ctrl+C to stop)")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
