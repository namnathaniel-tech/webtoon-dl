#!/usr/bin/env python3
"""Webtoon/manga downloader for comic.naver.com, webtoons.com and mangadex.org.

Only fetches episodes that are freely viewable without logging in or paying.
Locked / paid / adult-gated episodes are skipped.

CLI usage:
    python3 webtoon_dl.py URL [--start N] [--end N] [--format images|cbz|pdf] [-o DIR]
"""
from __future__ import annotations

import argparse
import html
import io
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
DEFAULT_OUT = os.path.expanduser("~/Downloads/Webtoons")
FORMATS = ("images", "cbz", "pdf")


class WebtoonError(Exception):
    pass


def _ssl_context():
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    ctx = ssl.create_default_context()
    if os.path.exists("/etc/ssl/cert.pem"):  # macOS system bundle (python.org builds ship none)
        ctx.load_verify_locations("/etc/ssl/cert.pem")
    return ctx


SSL_CTX = _ssl_context()


# ---------------------------------------------------------------- HTTP helpers

def fetch(url: str, referer: str | None = None, retries: int = 3, ua: str = UA) -> bytes:
    host = urllib.parse.urlparse(url).netloc
    if host.endswith(("mangadex.org", "mangadex.network")):
        ua = "webtoon-dl/1.0 (personal downloader)"  # MangaDex rejects spoofed browser UAs
    headers = {"User-Agent": ua, "Accept-Language": "ko,en;q=0.8"}
    if referer:
        headers["Referer"] = referer
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404):
                raise
            last = e
        except Exception as e:  # network hiccup
            last = e
        time.sleep(1 + attempt)
    raise WebtoonError(f"Failed to fetch {url}: {last}")


def fetch_text(url: str, referer: str | None = None) -> str:
    return fetch(url, referer).decode("utf-8", "replace")


def fetch_json(url: str, referer: str | None = None) -> dict:
    return json.loads(fetch_text(url, referer))


def safe_name(s: str, limit: int = 80) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", s).strip(" .")
    return (s[:limit].rstrip(" .")) or "untitled"


# ---------------------------------------------------------------- data

@dataclass
class Episode:
    no: int
    title: str
    locked: bool = False


@dataclass
class Series:
    site: str            # "naver" | "webtoons"
    title_id: str
    title: str
    url: str
    thumbnail: str = ""
    episodes: list[Episode] = field(default_factory=list)
    # site-specific
    naver_kind: str = "webtoon"       # webtoon | bestChallenge | challenge
    webtoons_path: str = ""           # e.g. /en/fantasy/tower-of-god
    chapter_ids: dict[int, str] = field(default_factory=dict)  # mangadex: no -> chapter uuid
    lang: str = ""                    # mangadex translation language
    languages: list[str] = field(default_factory=list)
    preselect: int | None = None      # episode the pasted link pointed at

    def to_dict(self) -> dict:
        return {
            "site": self.site, "titleId": self.title_id, "title": self.title,
            "url": self.url, "thumbnail": self.thumbnail, "lang": self.lang,
            "languages": self.languages, "preselect": self.preselect,
            "episodes": [e.__dict__ for e in self.episodes],
        }


# ---------------------------------------------------------------- site: comic.naver.com

def _naver_series(url: str, parsed: urllib.parse.ParseResult) -> Series:
    q = urllib.parse.parse_qs(parsed.query)
    if "titleId" not in q:
        raise WebtoonError("Naver link needs a titleId (open the series page and copy its URL).")
    tid = q["titleId"][0]
    kind = parsed.path.strip("/").split("/")[0] or "webtoon"
    if kind not in ("webtoon", "bestChallenge", "challenge"):
        kind = "webtoon"
    ref = "https://comic.naver.com/"
    try:
        info = fetch_json(f"https://comic.naver.com/api/article/list/info?titleId={tid}", ref)
    except urllib.error.HTTPError as e:
        raise WebtoonError(f"Naver refused this title (HTTP {e.code}). Adult titles need a login and aren't supported.")
    if info.get("adult"):
        raise WebtoonError("This is an adult-only title that requires a Naver login; not supported.")
    s = Series("naver", tid, info.get("titleName", f"title {tid}"), url,
               info.get("thumbnailUrl", ""), naver_kind=kind)

    def get_page(p: int) -> dict:
        return fetch_json(f"https://comic.naver.com/api/article/list?titleId={tid}&page={p}&sort=ASC", ref)

    first = get_page(1)
    total_pages = (first.get("pageInfo") or {}).get("totalPages") or 1
    with ThreadPoolExecutor(6) as pool:
        pages = [first] + list(pool.map(get_page, range(2, total_pages + 1)))
    eps: dict[int, Episode] = {}
    for data in pages:
        for a in data.get("articleList", []):
            locked = bool(a.get("charge")) or bool(a.get("thumbnailLock"))
            eps[a["no"]] = Episode(a["no"], a.get("subtitle", ""), locked)
    s.episodes = sorted(eps.values(), key=lambda e: e.no)
    return s


def _naver_images(s: Series, no: int) -> tuple[list[str], str]:
    page = f"https://comic.naver.com/{s.naver_kind}/detail?titleId={s.title_id}&no={no}"
    text = fetch_text(page, "https://comic.naver.com/")
    imgs = re.findall(r'<img\s+src="([^"]+)"[^>]*id="content_image_\d+"', text)
    if not imgs:  # attribute order fallback
        imgs = re.findall(r'id="content_image_\d+"[^>]*src="([^"]+)"', text)
    return [html.unescape(u) for u in imgs], page


# ---------------------------------------------------------------- site: webtoons.com

_WT_ITEM = re.compile(r'data-episode-no="(\d+)".*?<span class="subj"><span>(.*?)</span>', re.S)


def _webtoons_series(url: str, parsed: urllib.parse.ParseResult) -> Series:
    q = urllib.parse.parse_qs(parsed.query)
    if "title_no" not in q:
        raise WebtoonError("webtoons.com link needs a title_no (copy the series or episode URL).")
    tid = q["title_no"][0]
    parts = [p for p in parsed.path.split("/") if p]
    # /en/fantasy/tower-of-god/list  or  /en/fantasy/tower-of-god/<ep-slug>/viewer
    if len(parts) < 3:
        raise WebtoonError("Unrecognized webtoons.com URL.")
    base_path = "/" + "/".join(parts[:3])
    list_url = f"https://www.webtoons.com{base_path}/list?title_no={tid}"
    first = fetch_text(list_url, "https://www.webtoons.com/")
    m = re.search(r'<h1 class="subj">\s*(.*?)\s*</h1>', first, re.S)
    title = html.unescape(re.sub(r"<[^>]+>", " ", m.group(1))).strip() if m else f"title {tid}"
    title = re.sub(r"\s+", " ", title)
    thumb = re.search(r'<meta property="og:image" content="([^"]+)"', first)

    nos = [int(n) for n in re.findall(r'data-episode-no="(\d+)"', first)]
    if not nos:
        raise WebtoonError("No episodes found on that page.")
    latest = max(nos)
    per_page = max(len(set(nos)), 1)
    pages = math.ceil(latest / per_page)

    def get_page(p: int) -> str:
        return first if p == 1 else fetch_text(f"{list_url}&page={p}", "https://www.webtoons.com/")

    eps: dict[int, Episode] = {}
    with ThreadPoolExecutor(6) as pool:
        for text in pool.map(get_page, range(1, pages + 1)):
            for no, sub in _WT_ITEM.findall(text):
                eps[int(no)] = Episode(int(no), html.unescape(re.sub(r"<[^>]+>", "", sub)).strip())
    s = Series("webtoons", tid, title, url, thumb.group(1) if thumb else "", webtoons_path=base_path)
    s.episodes = sorted(eps.values(), key=lambda e: e.no)
    return s


def _webtoons_images(s: Series, no: int) -> tuple[list[str], str]:
    page = f"https://www.webtoons.com{s.webtoons_path}/ep/viewer?title_no={s.title_id}&episode_no={no}"
    text = fetch_text(page, "https://www.webtoons.com/")
    imgs = re.findall(r'class="_images"[^>]*data-url="([^"]+)"', text)
    return [html.unescape(u) for u in imgs], "https://www.webtoons.com/"


# ---------------------------------------------------------------- site: mangadex.org (official public API)

MD_API = "https://api.mangadex.org"
MD_UA = "webtoon-dl/1.0 (personal downloader)"   # MangaDex asks clients not to spoof browsers
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def _md(path: str) -> dict:
    try:
        return json.loads(fetch(MD_API + path, ua=MD_UA).decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise WebtoonError("MangaDex couldn't find that title/chapter.")
        if e.code == 429:
            raise WebtoonError("MangaDex rate limit hit; wait a minute and try again.")
        raise


def _mangadex_series(url: str, parsed: urllib.parse.ParseResult, lang: str | None) -> Series:
    m = re.search(rf"/(title|manga|chapter)/({_UUID})", parsed.path)
    if not m:
        raise WebtoonError("MangaDex link should look like mangadex.org/title/<id> or /chapter/<id>.")
    kind, uid = m.groups()
    chapter_id = None
    if kind == "chapter":
        chapter_id = uid
        ch = _md(f"/chapter/{uid}")["data"]
        uid = next(r["id"] for r in ch["relationships"] if r["type"] == "manga")
        lang = lang or ch["attributes"]["translatedLanguage"]

    d = _md(f"/manga/{uid}?includes[]=cover_art")["data"]
    a = d["attributes"]
    titles = a.get("title") or {}
    title = titles.get("en") or next(iter(titles.values()), f"manga {uid}")
    langs = sorted(l for l in (a.get("availableTranslatedLanguages") or []) if l)
    if not lang:
        lang = "en" if "en" in langs or not langs else langs[0]
    cover = next((r.get("attributes", {}).get("fileName") for r in d["relationships"]
                  if r["type"] == "cover_art"), None)
    thumb = f"https://uploads.mangadex.org/covers/{uid}/{cover}.256.jpg" if cover else ""

    raw, offset = [], 0
    ratings = "".join(f"&contentRating[]={r}" for r in ("safe", "suggestive", "erotica", "pornographic"))
    while True:
        q = (f"/manga/{uid}/feed?translatedLanguage[]={urllib.parse.quote(lang)}&limit=500&offset={offset}"
             f"&order[volume]=asc&order[chapter]=asc&includes[]=scanlation_group{ratings}")
        page = _md(q)
        raw += page.get("data", [])
        offset += 500
        if offset >= page.get("total", 0):
            break

    # Several groups often translate the same chapter: keep one per chapter number,
    # preferring a version that's hosted on MangaDex (pages > 0).
    best: dict[str, dict] = {}
    for c in raw:
        ca = c["attributes"]
        key = ca.get("chapter") or f"oneshot-{c['id']}"
        cur = best.get(key)
        if (cur is None or c["id"] == chapter_id
                or (cur["id"] != chapter_id and cur["attributes"]["pages"] == 0 and ca["pages"] > 0)):
            best[key] = c

    def sort_key(c):
        ch = c["attributes"].get("chapter")
        try:
            return (0, float(ch))
        except (TypeError, ValueError):
            return (1, 0.0)

    s = Series("mangadex", uid, title, url, thumb, lang=lang, languages=langs)
    for i, c in enumerate(sorted(best.values(), key=sort_key), start=1):
        ca = c["attributes"]
        group = next((r.get("attributes", {}).get("name") for r in c["relationships"]
                      if r["type"] == "scanlation_group"), None)
        label = f"Ch. {ca['chapter']}" if ca.get("chapter") else "Oneshot"
        if ca.get("title"):
            label += f" - {ca['title']}"
        if group:
            label += f" [{group}]"
        if ca["pages"] == 0 or ca.get("externalUrl"):
            label += " (official site only)"
        s.episodes.append(Episode(i, label, locked=ca["pages"] == 0 or bool(ca.get("externalUrl"))))
        s.chapter_ids[i] = c["id"]
        if chapter_id and c["id"] == chapter_id:
            s.preselect = i
    return s


def _mangadex_images(s: Series, no: int) -> tuple[list[str], str]:
    cid = s.chapter_ids[no]
    for attempt in range(3):
        try:
            h = _md(f"/at-home/server/{cid}")
            break
        except WebtoonError as e:
            if "rate limit" in str(e) and attempt < 2:
                time.sleep(15)
                continue
            raise
    base, ch = h["baseUrl"], h["chapter"]
    return [f"{base}/data/{ch['hash']}/{f}" for f in ch["data"]], ""


# ---------------------------------------------------------------- public API

def get_series(url: str, lang: str | None = None) -> Series:
    url = url.strip()
    if not re.match(r"https?://", url):
        url = "https://" + url
    p = urllib.parse.urlparse(url)
    host = p.netloc.lower()
    if host.endswith("comic.naver.com"):
        if host.startswith("m."):
            p = p._replace(netloc="comic.naver.com")
        return _naver_series(url, p)
    if host.endswith("webtoons.com"):
        return _webtoons_series(url, p)
    if host.endswith("mangadex.org"):
        return _mangadex_series(url, p, lang)
    raise WebtoonError("Supported sites: comic.naver.com, webtoons.com, mangadex.org.")


def episode_images(s: Series, no: int) -> tuple[list[str], str]:
    if s.site == "naver":
        return _naver_images(s, no)
    if s.site == "mangadex":
        return _mangadex_images(s, no)
    return _webtoons_images(s, no)


def _ext(url: str) -> str:
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
    return ext if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp") else ".jpg"


def download_episode(s: Series, ep: Episode, out_root: str, fmt: str,
                     on_image=None) -> str:
    """Download one episode. Returns the path written."""
    urls, referer = episode_images(s, ep.no)
    if not urls:
        raise WebtoonError("no images (locked, paid, or login-only episode)")
    series_dir = os.path.join(out_root, safe_name(s.title))
    os.makedirs(series_dir, exist_ok=True)
    name = safe_name(f"{ep.no:04d} - {ep.title}" if ep.title else f"{ep.no:04d}")

    def grab(item):
        i, u = item
        data = fetch(u, referer)
        if on_image:
            on_image()
        return i, u, data

    with ThreadPoolExecutor(6) as pool:
        results = sorted(pool.map(grab, enumerate(urls)))

    if fmt == "images":
        d = os.path.join(series_dir, name)
        os.makedirs(d, exist_ok=True)
        for i, u, data in results:
            with open(os.path.join(d, f"{i + 1:03d}{_ext(u)}"), "wb") as f:
                f.write(data)
        return d
    if fmt == "cbz":
        path = os.path.join(series_dir, name + ".cbz")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
            for i, u, data in results:
                z.writestr(f"{i + 1:03d}{_ext(u)}", data)
        return path
    if fmt == "pdf":
        try:
            from PIL import Image
        except ImportError:
            raise WebtoonError("PDF needs Pillow: run `pip install Pillow` (or use ./run.sh)")
        pages = [Image.open(io.BytesIO(data)).convert("RGB") for _, _, data in results]
        path = os.path.join(series_dir, name + ".pdf")
        pages[0].save(path, save_all=True, append_images=pages[1:], resolution=96)
        return path
    raise WebtoonError(f"unknown format {fmt}")


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Download free episodes from Naver Webtoon, webtoons.com and MangaDex")
    ap.add_argument("url")
    ap.add_argument("--start", type=int, help="first episode number (default: first)")
    ap.add_argument("--end", type=int, help="last episode number (default: last)")
    ap.add_argument("--format", choices=FORMATS, default="images")
    ap.add_argument("-o", "--out", default=DEFAULT_OUT)
    ap.add_argument("--list", action="store_true", help="just list episodes")
    ap.add_argument("--lang", help="MangaDex translation language, e.g. en, ko, es (default: en)")
    a = ap.parse_args(argv)

    try:
        s = get_series(a.url, a.lang)
    except (WebtoonError, urllib.error.URLError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"{s.title}  ({len(s.episodes)} episodes, {s.site})")
    if a.list:
        for e in s.episodes:
            print(f"  {e.no:5d}  {e.title}{'  [locked]' if e.locked else ''}")
        return 0
    eps = [e for e in s.episodes
           if (a.start is None or e.no >= a.start) and (a.end is None or e.no <= a.end)]
    for e in eps:
        if e.locked:
            print(f"  skip {e.no} (locked/paid)")
            continue
        try:
            path = download_episode(s, e, a.out, a.format)
            print(f"  ok   {e.no}  -> {path}")
        except Exception as ex:
            print(f"  fail {e.no}: {ex}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
