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

import webtoon_dl as wd

HOST, PORT = "127.0.0.1", int(os.environ.get("WEBTOON_PORT", 8765))
HERE = os.path.dirname(os.path.abspath(__file__))

_series_cache: dict[str, wd.Series] = {}
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def run_job(job_id: str, s: wd.Series, nos: list[int], fmt: str, out: str) -> None:
    job = _jobs[job_id]
    eps = [e for e in s.episodes if e.no in set(nos)]
    for ep in eps:
        if job["cancel"]:
            job["status"] = "cancelled"
            return
        item = {"no": ep.no, "title": ep.title, "state": "downloading", "images": 0, "path": "", "error": ""}
        with _lock:
            job["items"].append(item)
            job["current"] = ep.no

        def bump(item=item):
            with _lock:
                item["images"] += 1
        try:
            item["path"] = wd.download_episode(s, ep, out, fmt, on_image=bump)
            item["state"] = "done"
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

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if self.path == "/api/config":
            return self._send(200, {"outDir": wd.DEFAULT_OUT, "pdf": _has_pil()})
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
                ref = "https://www.webtoons.com/" if "webtoon-phinf" in u else "https://comic.naver.com/"
                return self._send(200, wd.fetch(u, ref), "image/jpeg")
            except Exception:
                return self._send(404, {"error": "thumb failed"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            body = self._body()
        except Exception:
            return self._send(400, {"error": "bad json"})
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
            threading.Thread(target=run_job, args=(job_id, s, nos, fmt, out), daemon=True).start()
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
