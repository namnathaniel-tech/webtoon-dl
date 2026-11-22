"""Reads downloaded series from disk for the reader.

Layout (what webtoon_dl writes):
    <root>/<Series>/<0001 - Title>/001.jpg ...        image folder
    <root>/<Series>/<0001 - Title>.cbz                 comic archive
    <root>/<Series>/<0001 - Title [EN]>/...            translated version
PDFs are skipped (a browser can't show them as a scroll of images).
"""
from __future__ import annotations

import functools
import os
import re
import zipfile

from PIL import Image

IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")
EN_TAG = " [EN]"
COVER = "cover.jpg"   # series cover art saved by webtoon_dl.save_cover


def read_cover(root: str, series: str) -> bytes:
    p = os.path.join(root, series, COVER)
    if not _inside(root, p) or not os.path.isfile(p):
        raise FileNotFoundError(series)
    with open(p, "rb") as f:
        return f.read()


# ---------------------------------------------------------------- AniList (series info)

ANILIST_Q = """query($s:String){Page(perPage:8){media(search:$s,type:MANGA){id
  title{romaji english native} synonyms countryOfOrigin format status chapters averageScore
  genres siteUrl startDate{year} description(asHtml:false) coverImage{extraLarge}}}}"""


def _key(t: str) -> str:
    return re.sub(r"[\W_]+", "", t or "").lower()


def anilist_lookup(title: str) -> dict | None:
    """Series info from AniList's public API for a title (Korean, Japanese or English).
    Only an exact title match is accepted, so a wrong series is never attached."""
    import json
    import urllib.request
    import webtoon_dl as wd
    body = json.dumps({"query": ANILIST_Q, "variables": {"s": title}}).encode()
    req = urllib.request.Request("https://graphql.anilist.co", body, {
        "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "webtoon-dl/1.0"})
    with urllib.request.urlopen(req, timeout=20, context=wd.SSL_CTX) as r:
        media = json.load(r)["data"]["Page"]["media"]
    want = _key(title)
    for m in media:
        names = [m["title"].get(k) for k in ("romaji", "english", "native")] + (m.get("synonyms") or [])
        if any(_key(n) == want for n in names if n):
            desc = re.sub(r"<[^>]+>", "", m.get("description") or "")
            desc = re.sub(r"\n{3,}", "\n\n", desc).strip()
            return {"id": m["id"], "english": m["title"].get("english"), "romaji": m["title"].get("romaji"),
                    "native": m["title"].get("native"), "country": m.get("countryOfOrigin"),
                    "format": m.get("format"), "status": m.get("status"), "chapters": m.get("chapters"),
                    "score": m.get("averageScore"), "genres": m.get("genres") or [],
                    "year": (m.get("startDate") or {}).get("year"), "url": m.get("siteUrl"),
                    "description": desc, "cover": (m.get("coverImage") or {}).get("extraLarge")}
    return None


def _read_meta(d: str) -> dict:
    import json
    try:
        with open(os.path.join(d, "series.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def backfill_anilist(root: str) -> int:
    """Look up AniList info once for every series that hasn't been looked up yet."""
    import json
    added = 0
    if not os.path.isdir(root):
        return 0
    for name in os.listdir(root):
        d = os.path.join(root, name)
        if name.startswith(".") or not os.path.isdir(d) or not _entries(d):
            continue
        meta = _read_meta(d)
        if "anilist" in meta:        # already looked up (None = not on AniList)
            continue
        try:
            info = anilist_lookup(meta.get("title") or name)
        except Exception:
            continue                 # offline / rate-limited: try again next time
        meta["anilist"] = info
        with open(os.path.join(d, "series.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        if info and info.get("cover") and not os.path.isfile(os.path.join(d, COVER)):
            try:
                import webtoon_dl as wd
                with open(os.path.join(d, COVER), "wb") as f:
                    f.write(wd.fetch(info["cover"]))
            except Exception:
                pass
        added += 1
    return added


def backfill_covers(root: str) -> int:
    """Fetch cover art for series downloaded before covers were saved (needs series.json or a
    MangaDex title match). Returns how many covers were added."""
    import json
    import webtoon_dl as wd
    added = 0
    if not os.path.isdir(root):
        return 0
    for name in os.listdir(root):
        d = os.path.join(root, name)
        if name.startswith(".") or not os.path.isdir(d) or os.path.isfile(os.path.join(d, COVER)):
            continue
        if not _entries(d):
            continue
        url = None
        meta = os.path.join(d, "series.json")
        try:
            if os.path.isfile(meta):
                with open(meta, encoding="utf-8") as f:
                    url = json.load(f).get("cover")
            if not url:
                url = wd.mangadex_cover_by_title(name)
            if url:
                ref = "https://comic.naver.com/" if "pstatic" in url else None
                with open(os.path.join(d, COVER), "wb") as f:
                    f.write(wd.fetch(url, ref))
                added += 1
        except Exception:
            continue
    return added


def _natural(s: str):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", s)]


def _inside(root: str, path: str) -> bool:
    root, path = os.path.realpath(root), os.path.realpath(path)
    return path == root or path.startswith(root + os.sep)


def _entries(series_dir: str) -> list[str]:
    out = []
    for name in os.listdir(series_dir):
        if name.startswith("."):
            continue
        p = os.path.join(series_dir, name)
        if os.path.isdir(p) and any(f.lower().endswith(IMG_EXT) for f in os.listdir(p)):
            out.append(name)
        elif name.lower().endswith(".cbz"):
            out.append(name)
    return sorted(out, key=_natural)


def _base(entry: str) -> tuple[str, bool]:
    name = entry[:-4] if entry.lower().endswith(".cbz") else entry
    if name.endswith(EN_TAG):
        return name[: -len(EN_TAG)], True
    return name, False


def list_library(root: str) -> list[dict]:
    if not os.path.isdir(root):
        return []
    series = []
    for name in sorted(os.listdir(root), key=_natural):
        d = os.path.join(root, name)
        if name.startswith(".") or not os.path.isdir(d):
            continue
        eps: dict[str, dict] = {}
        for e in _entries(d):
            base, en = _base(e)
            ep = eps.setdefault(base, {"id": base, "title": base, "original": None, "english": None})
            ep["english" if en else "original"] = e
        if not eps:
            continue
        ordered = sorted(eps.values(), key=lambda x: _natural(x["id"]))
        for ep in ordered:  # "0012 - Ch. 10" -> "Ch. 10"
            m = re.match(r"^\d+\s*-\s*(.+)$", ep["id"])
            ep["title"] = m.group(1) if m else ep["id"]
        first = ordered[0]
        art = os.path.join(d, COVER)
        series.append({
            "name": name,
            "episodes": ordered,
            "cover": first["original"] or first["english"],
            "art": int(os.path.getmtime(art)) if os.path.isfile(art) else 0,  # real cover art, if saved
            "info": _read_meta(d).get("anilist"),                             # AniList series info
            "updated": max(os.path.getmtime(os.path.join(d, e)) for e in _entries(d)),
        })
    series.sort(key=lambda s: -s["updated"])
    return series


@functools.lru_cache(maxsize=64)
def _zip_names(path: str, mtime: float) -> tuple[str, ...]:
    with zipfile.ZipFile(path) as z:
        return tuple(sorted((n for n in z.namelist() if n.lower().endswith(IMG_EXT)
                             and not n.startswith("__MACOSX")), key=_natural))


@functools.lru_cache(maxsize=4096)
def _size(path: str, member: str, mtime: float) -> tuple[int, int]:
    """Image width/height from the header only (cheap), so the reader can reserve space."""
    try:
        if member:
            with zipfile.ZipFile(path) as z, z.open(member) as f:
                return Image.open(f).size
        return Image.open(path).size
    except Exception:
        return (800, 1200)


def pages(root: str, series: str, entry: str) -> list[dict]:
    d = os.path.join(root, series)
    p = os.path.join(d, entry)
    if not _inside(root, p) or not os.path.exists(p):
        raise FileNotFoundError(entry)
    out = []
    if os.path.isdir(p):
        for f in sorted((f for f in os.listdir(p) if f.lower().endswith(IMG_EXT)), key=_natural):
            fp = os.path.join(p, f)
            w, h = _size(fp, "", os.path.getmtime(fp))
            out.append({"file": f, "w": w, "h": h})
    else:
        mt = os.path.getmtime(p)
        for n in _zip_names(p, mt):
            w, h = _size(p, n, mt)
            out.append({"file": n, "w": w, "h": h})
    return out


def read_page(root: str, series: str, entry: str, file: str) -> tuple[bytes, str]:
    p = os.path.join(root, series, entry)
    if not _inside(root, p):
        raise FileNotFoundError(entry)
    ext = os.path.splitext(file)[1].lower()
    ctype = {".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}.get(ext, "image/jpeg")
    if os.path.isdir(p):
        fp = os.path.join(p, file)
        if not _inside(p, fp) or not os.path.isfile(fp):
            raise FileNotFoundError(file)
        with open(fp, "rb") as f:
            return f.read(), ctype
    if file not in _zip_names(p, os.path.getmtime(p)):
        raise FileNotFoundError(file)
    with zipfile.ZipFile(p) as z:
        return z.read(file), ctype
