"""Translate a downloaded episode into English and letter it onto the pages.

Pipeline (follows the KR Translation Workflow / Lettering notes):
  1. Slice the episode into tiles. Webtoon strips are cut at blank gutters between
     panels so balloons aren't split; manga pages stay one page per tile.
  2. Send tiles to Claude a few at a time, with the series glossary and the last lines
     translated, and get back every text region (boxes + English) as JSON.
  3. Erase the source text and letter the English: all caps, diamond-shaped line
     breaks, one consistent size per episode, bold italic for emphasis.
     SFX are subtitled next to the art instead of erased (keeps the drawing intact).
  4. Write a script (original -> English) next to the episode, and grow the glossary.
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import re
import statistics

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = "claude-opus-5-5"
EFFORT = "medium"
TILES_PER_REQUEST = 4
GROQ_TILES_PER_REQUEST = 1   # free Groq plan: ~7k input and 1k output tokens per minute
GROQ_MAX_OUT = 1000
GROQ_MAX_PX = 700_000         # smaller images = fewer tokens on Groq
FONT_REG = os.path.join(HERE, "fonts", "ComicNeue-Bold.ttf")
FONT_EMPH = os.path.join(HERE, "fonts", "ComicNeue-BoldItalic.ttf")

with open(os.path.join(HERE, "translation_guide.md"), encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read()

BOX = {"type": "array", "items": {"type": "integer"}}
SCHEMA = {
    "type": "object",
    "properties": {
        "tiles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tile": {"type": "integer"},
                    "regions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {"type": "string", "enum": ["speech", "thought", "narration", "sfx", "sign"]},
                                "original": {"type": "string"},
                                "english": {"type": "string"},
                                "text_box": BOX,
                                "area_box": BOX,
                            },
                            "required": ["kind", "original", "english", "text_box", "area_box"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["tile", "regions"],
                "additionalProperties": False,
            },
        },
        "glossary_additions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "english": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["source", "english", "note"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["tiles", "glossary_additions"],
    "additionalProperties": False,
}


class TranslateError(Exception):
    pass


# Groq (OpenAI-compatible API) is used when a Groq key is set; otherwise Claude.
# The key comes from GROQ_API_KEY or a private file outside the repo (never commit it).
GROQ_MODEL = os.environ.get("GROQ_MODEL", "qwen/qwen3.8-27b")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_KEY_FILE = os.path.expanduser("~/Library/Application Support/webtoon-dl/groq.key")


def groq_key() -> str:
    k = os.environ.get("GROQ_API_KEY", "").strip()
    if not k:
        try:
            with open(GROQ_KEY_FILE, encoding="utf-8") as f:
                k = f.read().strip()
        except OSError:
            k = ""
    return k


DEEPL_KEY_FILE = os.path.expanduser("~/Library/Application Support/webtoon-dl/deepl.key")
DEEPL_LANGS = {"ko": "KO", "ja": "JA", "zh": "ZH", "zh-hant": "ZH", "zh-hk": "ZH", "id": "ID", "es": "ES",
               "es-la": "ES", "fr": "FR", "de": "DE", "pt-br": "PT", "ru": "RU", "pl": "PL", "tr": "TR",
               "it": "IT", "th": "TH", "vi": "VI"}


def save_deepl_key(key: str) -> None:
    """Check the key with DeepL, then store it privately (0600) outside the repo. '' removes it."""
    import urllib.error
    import urllib.request
    key = key.strip()
    if not key:
        try:
            os.remove(DEEPL_KEY_FILE)
        except OSError:
            pass
        return
    host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
    req = urllib.request.Request(f"https://{host}/v2/usage", headers={
        "Authorization": "DeepL-Auth-Key " + key, "User-Agent": "webtoon-dl/1.0"})
    import ssl
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    try:
        urllib.request.urlopen(req, timeout=20, context=ctx).read()
    except urllib.error.HTTPError as e:
        raise TranslateError("DeepL didn't accept that key." if e.code in (401, 403)
                             else f"DeepL error {e.code}; try again.")
    except urllib.error.URLError:
        raise TranslateError("Couldn't reach DeepL to check the key (network).")
    os.makedirs(os.path.dirname(DEEPL_KEY_FILE), exist_ok=True)
    fd = os.open(DEEPL_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key + "\n")


def deepl_key() -> str:
    k = os.environ.get("DEEPL_API_KEY", "").strip()
    if not k:
        try:
            with open(DEEPL_KEY_FILE, encoding="utf-8") as f:
                k = f.read().strip()
        except OSError:
            k = ""
    return k


def provider() -> str:
    """deepl (OCR + DeepL text translation) > groq (vision LLM) > anthropic (Claude)."""
    if deepl_key():
        return "deepl"
    if groq_key():
        return "groq"
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "anthropic"
    return ""


def available() -> bool:
    return bool(provider())


# ---------------------------------------------------------------- 1. tiles

def make_tiles(images: list[Image.Image], strip: bool) -> list[Image.Image]:
    images = [im.convert("RGB") for im in images]
    if not strip:
        return images
    width = statistics.mode(im.width for im in images)
    images = [im if im.width == width else im.resize((width, round(im.height * width / im.width)))
              for im in images]
    total = sum(im.height for im in images)
    strip_img = Image.new("RGB", (width, total), "white")
    y = 0
    for im in images:
        strip_img.paste(im, (0, y))
        y += im.height

    gray = np.asarray(strip_img.convert("L"), dtype=np.int16)
    row_range = gray.max(axis=1) - gray.min(axis=1)       # 0 for a perfectly flat row
    blank = row_range <= 6
    # only cut in real gaps between panels, never in the small gap between two lines of text
    # (cutting there splits a caption in half and each half gets lettered on its own)
    min_gap = max(40, int(width * 0.08))
    run = np.zeros(total, dtype=np.int32)
    i = 0
    while i < total:
        if blank[i]:
            j = i
            while j < total and blank[j]:
                j += 1
            run[i:j] = j - i
            i = j
        else:
            i += 1
    blank = blank & (run >= min_gap)

    target, lo, hi = int(width * 1.9), int(width * 1.1), int(width * 2.6)
    cuts, start = [0], 0
    while total - start > hi:
        win = np.arange(start + lo, min(start + hi, total))
        cand = win[blank[win]]
        if len(cand):
            # prefer the middle of the blank run nearest the target height
            c = cand[np.argmin(np.abs(cand - (start + target)))]
            a = b = c
            while a - 1 >= start + lo and blank[a - 1]:
                a -= 1
            while b + 1 < min(start + hi, total) and blank[b + 1]:
                b += 1
            cut = (a + b) // 2
        else:
            cut = int(win[np.argmin(row_range[win])])  # least busy row
        cuts.append(cut)
        start = cut
    cuts.append(total)
    return [strip_img.crop((0, a, width, b)) for a, b in zip(cuts, cuts[1:]) if b - a > 4]


def _encode(im: Image.Image, max_px: int = 1_150_000) -> tuple[str, float]:
    """JPEG-encode for the API, downscaled to what vision uses (<=1568 px, ~1.15 MP)."""
    w, h = im.size
    scale = min(1.0, 1568 / max(w, h), math.sqrt(max_px / (w * h)))
    if scale < 1:
        im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return base64.standard_b64encode(buf.getvalue()).decode(), scale


# ---------------------------------------------------------------- 2. Claude

LANG_NAMES = {"ko": "Korean", "ja": "Japanese", "zh": "Chinese (Simplified)", "zh-hant": "Chinese (Traditional)",
              "zh-hk": "Chinese (Hong Kong / Cantonese)", "th": "Thai", "id": "Indonesian", "es": "Spanish (Spain)",
              "es-la": "Spanish (Latin America)", "fr": "French", "de": "German", "pt-br": "Portuguese (Brazil)",
              "vi": "Vietnamese", "ru": "Russian", "pl": "Polish", "tr": "Turkish", "it": "Italian"}


def _ask(client, tiles: list[Image.Image], first_no: int, series: str,
         glossary: dict, recent: list[str], source_lang: str = "") -> dict:
    content, scales = [], {}
    for i, t in enumerate(tiles):
        data, scale = _encode(t, GROQ_MAX_PX if client is None else 1_150_000)
        no = first_no + i
        scales[no] = scale
        w, h = round(t.width * scale), round(t.height * scale)
        content.append({"type": "text", "text": f"Tile {no} ({w}x{h} px):"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}})
    gl = "\n".join(f"- {k} = {v}" for k, v in glossary.items()) or "(empty — this is the start of the series)"
    rc = "\n".join(recent[-30:]) or "(none — this is the start of the episode)"
    content.append({"type": "text", "text":
        f"Series: {series}\n"
        + (f"Source language: {LANG_NAMES.get(source_lang, source_lang)}\n" if source_lang else "")
        + f"\nGlossary:\n{gl}\n\nMost recent lines already translated:\n{rc}\n\n"
        f"Translate every text region on tiles {first_no}–{first_no + len(tiles) - 1}. "
        "Boxes are in each tile's own pixel size shown above."})

    if client is None:   # Groq
        data = _ask_groq(content)
    else:
        data = _ask_claude(client, content)
    if data.get("_refused"):
        return data
    # boxes back to full-resolution tile pixels
    for t in data.get("tiles", []):
        s = scales.get(t.get("tile"), 1.0)
        for r in t.get("regions", []):
            for k in ("text_box", "area_box"):
                b = (list(r.get(k) or []) + [0, 0, 0, 0])[:4]
                r[k] = [round(v / s) for v in b]
    return data


def _clean(data: dict) -> dict:
    """Groq's JSON mode doesn't enforce the schema: keep only well-formed regions."""
    tiles = []
    for t in data.get("tiles") or []:
        if not isinstance(t, dict) or not isinstance(t.get("tile"), int):
            continue
        regions = []
        for r in t.get("regions") or []:
            if not isinstance(r, dict) or not isinstance(r.get("english"), str):
                continue
            ok = all(isinstance(r.get(k), list) and len(r[k]) >= 4
                     and all(isinstance(v, (int, float)) for v in r[k][:4]) for k in ("text_box", "area_box"))
            if not ok:
                continue
            kind = r.get("kind") if r.get("kind") in ("speech", "thought", "narration", "sfx", "sign") else "speech"
            regions.append({"kind": kind, "original": str(r.get("original", "")), "english": r["english"],
                            "text_box": [int(v) for v in r["text_box"][:4]],
                            "area_box": [int(v) for v in r["area_box"][:4]]})
        tiles.append({"tile": t["tile"], "regions": regions})
    gl = [g for g in data.get("glossary_additions") or []
          if isinstance(g, dict) and isinstance(g.get("source"), str) and isinstance(g.get("english"), str)]
    return {"tiles": tiles, "glossary_additions": [{"source": g["source"], "english": g["english"],
                                                    "note": str(g.get("note", ""))} for g in gl]}


def _groq_guide(content: list[dict]) -> str:
    """The translation guide trimmed for Groq's small per-minute token budget: keep only the
    "not Korean" subsection that matches this episode's source language."""
    m = re.search(r"Source language: ([^\n]+)", content[-1]["text"])
    lang = (m.group(1) if m else "Korean").lower()
    head, sep, rest = SYSTEM_PROMPT.partition("# When the source is not Korean")
    if not sep:
        return SYSTEM_PROMPT
    section, sep2, tail = rest.partition("# Glossary")
    keep = ""
    if "korean" not in lang:
        paras = re.split(r"\n(?=\*\*)", section)
        want = "japanese" if "japanese" in lang else "chinese" if ("chinese" in lang or "cantonese" in lang) else "thai"
        keep = "# When the source is not Korean" + paras[0] + "".join(
            "\n" + p for p in paras[1:] if want in p[:80].lower()) + "\n\n"
    return head + keep + sep2 + tail


def _ask_groq(content: list[dict]) -> dict:
    import time
    import urllib.error
    import urllib.request
    msgs = []
    for c in content:   # Anthropic content blocks → OpenAI-style parts
        if c["type"] == "text":
            msgs.append({"type": "text", "text": c["text"]})
        else:
            src = c["source"]
            msgs.append({"type": "image_url",
                         "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"}})
    system = (_groq_guide(content) + "\n\n# Output\nReply with one JSON object only, no prose, matching this JSON "
              "Schema exactly:\n" + json.dumps(SCHEMA, separators=(",", ":")))
    body = {"model": GROQ_MODEL, "temperature": 0.3, "max_completion_tokens": GROQ_MAX_OUT,
            "response_format": {"type": "json_object"}, "reasoning_effort": "none",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": msgs}]}
    req_body = json.dumps(body).encode()
    import ssl
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    for attempt in range(6):
        req = urllib.request.Request(GROQ_URL, req_body, {
            "Authorization": "Bearer " + groq_key(), "Content-Type": "application/json",
            "User-Agent": "webtoon-dl/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=180, context=ctx) as r:
                resp = json.load(r)
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code == 401:
                raise TranslateError("Groq API key was rejected; check GROQ_API_KEY or the saved key file.")
            if e.code == 429 and "Request too large" not in detail and attempt < 5:
                time.sleep(min(90.0, float(e.headers.get("retry-after") or 20)) + 1)   # per-minute limit
                continue
            if e.code == 400 and "json_validate_failed" in detail and attempt < 3:
                continue   # model produced invalid JSON; ask again
            raise TranslateError(f"Groq API error {e.code}: {detail}")
        except urllib.error.URLError:
            raise TranslateError("Couldn't reach the Groq API (network).")
    else:
        raise TranslateError("Groq kept failing; try again in a minute.")
    choice = resp["choices"][0]
    if choice.get("finish_reason") == "length":
        # too much text for the free plan's 1k-token answer: skip this tile, keep going
        return {"tiles": [], "glossary_additions": [], "_cut": True}
    text = choice["message"].get("content") or "{}"
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return _clean(json.loads(text))
    except ValueError:
        raise TranslateError("Groq returned something that isn't JSON.")


# ---------------------------------------------------------------- 2b. OCR snapping (Groq)
#
# Groq's vision model translates well but places boxes poorly. macOS's Vision framework
# finds text lines exactly, so each translated region is moved onto the OCR lines that
# spell its original text. Regions that match nothing are kept in the script but not lettered.

def _norm(t: str) -> str:
    return re.sub(r"[\s\W_]+", "", t or "").lower()


def _ocr_lines(tile: Image.Image) -> list[tuple[str, list[int]]] | None:
    try:
        import Vision
        from Foundation import NSData
    except ImportError:
        return None
    buf = io.BytesIO()
    tile.convert("RGB").save(buf, "JPEG", quality=92)
    raw = buf.getvalue()
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLanguages_(["ko-KR", "ja-JP", "zh-Hans", "zh-Hant", "en-US"])
    req.setRecognitionLevel_(0)          # accurate
    req.setUsesLanguageCorrection_(False)
    h = Vision.VNImageRequestHandler.alloc().initWithData_options_(
        NSData.dataWithBytes_length_(raw, len(raw)), None)
    ok, _ = h.performRequests_error_([req], None)
    if not ok:
        return None
    W, H = tile.size
    out = []
    for o in req.results() or []:
        c = o.topCandidates_(1)
        if not c:
            continue
        b = o.boundingBox()   # normalised, origin bottom-left
        x0, x1 = b.origin.x * W, (b.origin.x + b.size.width) * W
        y1 = (1 - b.origin.y) * H
        y0 = y1 - b.size.height * H
        out.append((c[0].string(), [round(x0), round(y0), round(x1), round(y1)], float(c[0].confidence())))
    return out


def _match(line: str, text: str) -> float:
    """How much of an OCR line appears (in order) inside a region's original text, 0..1."""
    if not line:
        return 0.0
    if line in text:
        return 1.0
    import difflib
    m = difflib.SequenceMatcher(None, line, text, autojunk=False)
    return sum(b.size for b in m.get_matching_blocks()) / len(line)


def _snap_to_ocr(tile: Image.Image, regions: list[dict]) -> None:
    lines = _ocr_lines(tile)
    if lines is None:
        return   # no Vision framework: keep the model's boxes
    norm = [(_norm(t), b) for t, b, *_ in lines]
    used: set[int] = set()
    for r in regions:
        text = _norm(r.get("original", ""))
        cands = [i for i, (t, _) in enumerate(norm) if i not in used and len(t) >= 1 and _match(t, text) >= 0.7]
        if not text or not cands:
            r["_noplace"] = True
            continue
        # seed: the unused line covering most of the text (topmost on ties), then grow
        # with matching lines stacked right above/below it (a balloon's other lines)
        seed = max(cands, key=lambda i: (len(norm[i][0]) * _match(norm[i][0], text), -norm[i][1][1]))
        group = [seed]
        box = list(norm[seed][1])
        lh = max(8, box[3] - box[1])
        grew = True
        while grew:
            grew = False
            for i in cands:
                if i in group:
                    continue
                b = norm[i][1]
                near_v = b[1] < box[3] + 1.6 * lh and b[3] > box[1] - 1.6 * lh
                over_h = min(b[2], box[2]) - max(b[0], box[0]) > -0.5 * lh
                if near_v and over_h:
                    group.append(i)
                    box = [min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]), max(box[3], b[3])]
                    grew = True
            if sum(len(norm[i][0]) for i in group) >= len(text):
                break
        used.update(group)
        pad = round(0.15 * lh)
        r["text_box"] = [box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad]
        if r.get("kind") in ("sfx", "sign"):
            r["area_box"] = list(r["text_box"])
        else:   # the balloon finder grows this to the real outline
            r["area_box"] = [box[0] - lh, box[1] - lh, box[2] + lh, box[3] + lh]


# ---------------------------------------------------------------- 2c. DeepL (OCR + text translation)
#
# DeepL only translates text, so the page is read with macOS's Vision OCR: lines are grouped
# into balloons by position, each balloon's text goes to DeepL in one batch, and the existing
# lettering step erases and re-letters it. No translation guide or glossary: DeepL gets the
# series title and the latest lines as context instead.

def _line_bg(arr: np.ndarray, box) -> tuple[tuple, float]:
    """Dominant colour around a text line and how much of the ring it covers (1.0 = flat)."""
    ring = _ring(arr, box, 6)
    if not ring.size:
        return (255, 255, 255), 0.0
    return _dominant(ring)


def _group_lines(lines: list[tuple], arr: np.ndarray | None = None) -> list[tuple[list[str], list[int]]]:
    """Merge OCR lines that sit in the same balloon/caption: stacked, similar size, and either
    centred on each other (balloons) or left-aligned and tightly spaced (captions).
    Low-confidence lines (usually stylised sound effects misread as words) are dropped."""
    kept = []
    for text, b, *rest in lines:
        conf = rest[0] if rest else 1.0
        core = _norm(text)
        bg, flat = _line_bg(arr, b) if arr is not None else ((255, 255, 255), 1.0)
        # unsure reads on busy artwork are nearly always stylised sound effects; on a flat
        # balloon or page they're usually real text with punctuation the OCR found odd ("…이건,")
        busy = flat < 0.5 or sum(bg) < 600     # artwork or a non-white background, not a balloon
        if conf < 0.25 or (busy and (conf < 0.4 or (conf <= 0.5 and len(core) <= 3))):
            continue
        kept.append((text, b, bg))
    items = sorted(kept, key=lambda l: (l[1][1], l[1][0]))
    groups: list[list] = []   # [texts, box, last_line_box, bg]
    for text, b, bg in items:
        h = max(1, b[3] - b[1])
        best, best_gap = None, None
        for g in groups:
            lb = g[2]
            lh = max(1, lb[3] - lb[1])
            gap = b[1] - lb[3]
            if not (0.6 < h / lh < 1.65) or not (-0.5 * lh < gap < 0.6 * lh):
                continue
            w = max(b[2] - b[0], lb[2] - lb[0])
            centred = abs((b[0] + b[2]) / 2 - (lb[0] + lb[2]) / 2) < 0.2 * w + 0.3 * lh
            left = abs(b[0] - lb[0]) < 0.4 * lh and gap < 0.35 * lh
            if (centred or left) and (best is None or gap < best_gap):
                best, best_gap = g, gap
        if best:
            gb = best[1]
            best[0].append(text)
            best[1] = [min(gb[0], b[0]), min(gb[1], b[1]), max(gb[2], b[2]), max(gb[3], b[3])]
            best[2] = list(b)
            best[3].append((text, list(b), bg))
        else:
            groups.append([[text], list(b), list(b), [(text, list(b), bg)]])
    return [(g[0], g[1], g[3]) for g in groups]


def _bg_segments(lines: list[tuple]) -> list[list[tuple]]:
    """Split a group's lines where the background changes (e.g. white page → black bar)."""
    segs: list[list[tuple]] = []
    for ln in lines:
        if segs and sum(abs(int(a) - int(b)) for a, b in zip(ln[2], segs[-1][-1][2])) <= 90:
            segs[-1].append(ln)
        else:
            segs.append([ln])
    return segs


def _split_english(en: str, weights: list[int]) -> list[str]:
    """Share one translated sentence between several places, in proportion to the original
    text in each, breaking after punctuation when one is close to the ideal point."""
    words = en.split()
    if len(weights) < 2 or len(words) < len(weights):
        return [en] + [""] * (len(weights) - 1)
    total = sum(weights) or 1
    parts, start, acc = [], 0, 0
    lens = [len(w) + 1 for w in words]
    full = sum(lens)
    for wgt in weights[:-1]:
        acc += wgt
        target = full * acc / total
        best, best_score = None, None
        pos = sum(lens[:start])
        for i in range(start + 1, len(words) - (len(weights) - len(parts) - 1) + 0):
            pos += lens[i - 1]
            score = abs(pos - target) - (0.25 * full / len(weights) if re.search(r"[,.;:!?…—]$", words[i - 1]) else 0)
            if best_score is None or score < best_score:
                best, best_score = i, score
        best = best or start + 1
        parts.append(" ".join(words[start:best]))
        start = best
    parts.append(" ".join(words[start:]))
    return parts


def _deepl(texts: list[str], source_lang: str, context: str) -> list[str]:
    import time
    import urllib.error
    import urllib.request
    key = deepl_key()
    host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
    out: list[str] = []
    import ssl
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    for i in range(0, len(texts), 50):   # DeepL takes up to 50 texts per request
        body = {"text": texts[i:i + 50], "target_lang": "EN-US", "context": context[-3000:]}
        src = DEEPL_LANGS.get(source_lang)
        if src:
            body["source_lang"] = src
        for attempt in range(5):
            req = urllib.request.Request(f"https://{host}/v2/translate", json.dumps(body).encode(), {
                "Authorization": "DeepL-Auth-Key " + key, "Content-Type": "application/json",
                "User-Agent": "webtoon-dl/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
                    out += [t["text"] for t in json.load(r)["translations"]]
                break
            except urllib.error.HTTPError as e:
                if e.code == 403:
                    raise TranslateError("DeepL rejected the API key; check DEEPL_API_KEY or the saved key file.")
                if e.code == 456:
                    raise TranslateError("DeepL's monthly character quota is used up.")
                if e.code in (429, 500, 503) and attempt < 4:
                    time.sleep(2 + 3 * attempt)
                    continue
                raise TranslateError(f"DeepL error {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
            except urllib.error.URLError:
                raise TranslateError("Couldn't reach DeepL (network).")
    return out


def _ask_deepl(tiles: list[Image.Image], first_no: int, series: str, recent: list[str],
               source_lang: str) -> dict:
    joiner = "" if source_lang in ("ja", "zh", "zh-hant", "zh-hk") else " "
    found = []   # (tile_no, original, box, line_height)
    for i, t in enumerate(tiles):
        lines = _ocr_lines(t)
        if lines is None:
            raise TranslateError("DeepL translation needs macOS text recognition (pyobjc-framework-Vision).")
        for texts, box, glines in _group_lines(lines, np.asarray(t.convert("RGB"))):
            original = re.sub(r"[•·]{2,}|\.{3,}", "…", joiner.join(x.strip() for x in texts))
            if not re.search(r"[^\W\d_]", original):   # numbers/punctuation only
                continue
            lh = (box[3] - box[1]) / max(1, len(texts))
            found.append((first_no + i, original, box, lh, _bg_segments(glines)))
    if not found:
        return {"tiles": [], "glossary_additions": []}
    context = f"Dialogue from the comic \"{series}\". " + " ".join(r.split(" => ")[-1] for r in recent[-8:])
    english = _deepl([f[1] for f in found], source_lang, context)
    by_tile: dict[int, list] = {}
    for (no, original, box, lh, segs), en in zip(found, english):
        # one sentence over two backgrounds (white page + black bar): translate it whole,
        # then letter each part where its original part was
        pieces = _split_english(en, [len(_norm("".join(t for t, _, _ in sg))) for sg in segs]) \
            if len(segs) > 1 else [en]
        if _norm(en) == _norm(original):
            continue   # nothing to translate (names, @handles, numbers): leave the art as is
        for sg, piece in zip(segs, pieces):
            if not piece:
                continue
            sb = [min(b[0] for _, b, _ in sg), min(b[1] for _, b, _ in sg),
                  max(b[2] for _, b, _ in sg), max(b[3] for _, b, _ in sg)]
            if len(segs) == 1:
                sb = box
            g = round((sb[3] - sb[1]) / max(1, len(sg)))
            pad = round(0.15 * g)
            by_tile.setdefault(no, []).append({
                "kind": "speech", "original": original if len(segs) == 1 else joiner.join(t for t, _, _ in sg),
                "english": piece, "text_box": [sb[0] - pad, sb[1] - pad, sb[2] + pad, sb[3] + pad],
                "area_box": [sb[0] - g, sb[1] - g, sb[2] + g, sb[3] + g], "glyph": g})
    return {"tiles": [{"tile": n, "regions": r} for n, r in sorted(by_tile.items())], "glossary_additions": []}


def _ask_claude(client, content: list[dict]) -> dict:
    import anthropic
    try:
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.AuthenticationError:
        raise TranslateError("Anthropic API key was rejected; check ANTHROPIC_API_KEY.")
    except anthropic.PermissionDeniedError:
        raise TranslateError("This API key isn't allowed to use the model.")
    except anthropic.RateLimitError:
        raise TranslateError("Anthropic rate limit reached; try again in a minute.")
    except anthropic.APIConnectionError:
        raise TranslateError("Couldn't reach the Anthropic API (network).")
    except anthropic.APIStatusError as e:
        raise TranslateError(f"Anthropic API error {e.status_code}: {e.message}")

    if resp.stop_reason == "refusal":
        return {"tiles": [], "glossary_additions": [], "_refused": True}
    if resp.stop_reason == "max_tokens":
        raise TranslateError("Response was cut off (too much text in these tiles).")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return json.loads(text)


# ---------------------------------------------------------------- 3. lettering
#
# A "word" is a list of runs [(text, emphasized)], so "**going**?!" stays one word
# with a bold-italic run and a regular run and no space between them.

_font_cache: dict = {}


def _font(size: int, emph: bool) -> ImageFont.FreeTypeFont:
    key = (size, emph)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(FONT_EMPH if emph else FONT_REG, size)
    return _font_cache[key]


def _words(text: str) -> list[list[tuple[str, bool]]]:
    """All caps, split on spaces, **emphasis** markers turned into runs."""
    words, emph = [], False
    for token in text.strip().split():
        runs = []
        for part in re.split(r"(\*\*)", token):
            if part == "**":
                emph = not emph
            elif part:
                runs.append((part.upper(), emph))
        if runs:
            words.append(runs)
    return words


def _wlen(word, size) -> float:
    return sum(_font(size, e).getlength(t) for t, e in word)


def _wrap(words, size, w, h, oval: bool):
    """Greedy wrap into an oval (diamond) or a rectangle. Returns lines, or None if it won't fit."""
    if not words:
        return []
    sp = _font(size, False).getlength(" ")
    widths = [_wlen(wd, size) for wd in words]
    line_h = size * 1.12
    max_lines = max(1, int(h // line_h))

    def avail(li, n):
        if not oval:
            return w
        y = ((li + 0.5) / n - 0.5) * (n * line_h) / (h / 2)
        return w * math.sqrt(max(0.30, 1 - y * y))

    for n in range(1, max_lines + 1):
        lines, cur, cur_w, li, ok = [], [], 0.0, 0, True
        for wd, ww in zip(words, widths):
            need = ww if not cur else cur_w + sp + ww
            if need <= avail(li, n):
                cur.append(wd); cur_w = need
                continue
            if not cur or li + 1 >= n or ww > avail(li + 1, n):
                ok = False
                break
            lines.append(cur); li += 1
            cur, cur_w = [wd], ww
        if ok and cur:
            lines.append(cur)
            return lines
    return None


def _max_fit(words, w, h, oval, hi, lo):
    for size in range(hi, lo - 1, -1):
        if _wrap(words, size, w, h, oval) is not None:
            return size
    return lo


def _draw_lines(draw, lines, size, box, fill):
    x0, y0, x1, y1 = box
    sp = _font(size, False).getlength(" ")
    line_h = size * 1.12
    y = (y0 + y1) / 2 - len(lines) * line_h / 2
    for line in lines:
        lw = sum(_wlen(wd, size) for wd in line) + sp * (len(line) - 1)
        x = (x0 + x1) / 2 - lw / 2
        for wd in line:
            for t, e in wd:
                draw.text((x, y), t, font=_font(size, e), fill=fill)
                x += _font(size, e).getlength(t)
            x += sp
        y += line_h


def _clamp(b, w, h):
    x0, y0, x1, y1 = b
    x0, x1 = sorted((max(0, min(w, int(x0))), max(0, min(w, int(x1)))))
    y0, y1 = sorted((max(0, min(h, int(y0))), max(0, min(h, int(y1)))))
    return [x0, y0, x1, y1]


def _dominant(pixels: np.ndarray):
    """Most common colour (quantized), and the share of pixels close to it."""
    q = (pixels // 24).astype(np.int32)
    keys = q[:, 0] * 10000 + q[:, 1] * 100 + q[:, 2]
    vals, counts = np.unique(keys, return_counts=True)
    top = vals[np.argmax(counts)]
    color = np.median(pixels[keys == top], axis=0)
    share = float((np.abs(pixels.astype(int) - color).sum(axis=1) < 48).mean())
    return tuple(int(v) for v in color), share


def _ring(img: np.ndarray, box, pad: int = 8) -> np.ndarray:
    """Pixels in a band just outside box: the background, not the (possibly bold or coloured) text."""
    h, w = img.shape[:2]
    x0, y0, x1, y1 = [int(v) for v in box]
    X0, Y0, X1, Y1 = max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad)
    m = np.ones((Y1 - Y0, X1 - X0), bool)
    m[max(0, y0 - Y0):max(0, y1 - Y0), max(0, x0 - X0):max(0, x1 - X0)] = False
    return img[Y0:Y1, X0:X1][m].reshape(-1, 3)


def _find_balloon(arr: np.ndarray, tb, ab):
    """Flood-fill the balloon interior around the source text.

    First looks wide (the model's boxes can be a little off). If that leaks into open
    background or swallows a neighbouring balloon, retries with the model's balloon
    box as the wall. Returns (background colour, erase mask, letter box) in tile
    coordinates, or None when the text isn't on a flat balloon/caption background.
    """
    H, W = arr.shape[:2]
    ux0, uy0, ux1, uy1 = min(tb[0], ab[0]), min(tb[1], ab[1]), max(tb[2], ab[2]), max(tb[3], ab[3])
    ab_area = max(1, (ux1 - ux0) * (uy1 - uy0))
    pad = int(max(30, 0.6 * max(ux1 - ux0, uy1 - uy0)))
    wide = _clamp([ux0 - pad, uy0 - pad, ux1 + pad, uy1 + pad], W, H)
    tight = _clamp([ux0 - 8, uy0 - 8, ux1 + 8, uy1 + 8], W, H)
    def covered(res):
        """Share of the source text's ink this result would erase."""
        bg, erase, _ = res
        sub = arr[tb[1]:tb[3], tb[0]:tb[2]].astype(int)
        ink = np.abs(sub - np.array(bg)).sum(axis=2) >= 60
        return float(erase[tb[1]:tb[3], tb[0]:tb[2]][ink].mean()) if ink.any() else 1.0

    best, best_score = None, -1.0
    for R, walled in ((wide, False), (tight, True)):
        res = _flood(arr, tb, ab, R, walled)
        if res is None:
            continue
        box = res[2]
        if not walled and (box[2] - box[0]) * (box[3] - box[1]) > 2.2 * ab_area:
            continue  # merged with a neighbouring balloon: retry inside the model's box
        score = covered(res)
        if not walled and score >= 0.85:
            return res
        if score > best_score:
            best, best_score = res, score
    return best


def _flood(arr, tb, ab, R, walled):
    H, W = arr.shape[:2]
    crop = arr[R[1]:R[3], R[0]:R[2]]
    t = [tb[0] - R[0], tb[1] - R[1], tb[2] - R[0], tb[3] - R[1]]
    ring = _ring(crop, t, 8)
    if ring.size == 0:
        return None
    bg, share = _dominant(ring)
    if share < 0.45:
        return None
    near = np.abs(crop.astype(int) - np.array(bg)).sum(axis=2) < 60

    # seed: the background pixel closest to the centre of the text box
    ys, xs = np.nonzero(near[t[1]:t[3], t[0]:t[2]])
    if not len(ys):
        return None
    cy, cx = (t[3] - t[1]) / 2, (t[2] - t[0]) / 2
    k = int(np.argmin((ys - cy) ** 2 + (xs - cx) ** 2))
    seed = (int(xs[k] + t[0]), int(ys[k] + t[1]))

    m = Image.fromarray(np.where(near, 255, 0).astype(np.uint8)).copy()
    ImageDraw.floodfill(m, seed, 128)
    interior = np.array(m) == 128
    if not walled:
        border = np.concatenate([interior[0], interior[-1], interior[:, 0], interior[:, -1]])
        if border.mean() > 0.02:
            return None  # leaks into open background
    # holes inside the interior = the glyphs; fill them (the crop edge counts as outside)
    outside = Image.fromarray(np.pad(np.where(interior, 0, 255).astype(np.uint8), 1, constant_values=255)).copy()
    ImageDraw.floodfill(outside, (0, 0), 128)
    holes = (np.array(outside)[1:-1, 1:-1] == 255)
    if walled:
        # with walls, glyphs touching the wall aren't enclosed: take ink inside the text box too
        tm = np.zeros_like(holes)
        tm[max(0, t[1] - 3):t[3] + 3, max(0, t[0] - 3):t[2] + 3] = True
        holes |= tm & ~near
    balloon = interior | holes
    if balloon.sum() < 0.5 * (t[2] - t[0]) * (t[3] - t[1]):
        return None
    def dilate(mask, k):
        return np.asarray(Image.fromarray((mask * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(k))) > 0

    def erode(mask, k):
        return np.asarray(Image.fromarray((mask * 255).astype(np.uint8)).filter(ImageFilter.MinFilter(k))) > 0

    # Glyphs touching an outline (or in a balloon with gaps) aren't enclosed holes. Close the
    # balloon shape to cover them, pull it 2 px in from the outline, and erase any ink inside
    # it that lies near where the model says the text and balloon are.
    closed = erode(erode(dilate(balloon, 31), 31), 3)
    zone = np.zeros_like(balloon)
    a = [ab[0] - R[0], ab[1] - R[1], ab[2] - R[0], ab[3] - R[1]]
    # (the model's balloon box isn't used here: it can reach a neighbouring outline)
    zone[max(0, t[1] - 14):t[3] + 14, max(0, t[0] - 14):t[2] + 14] = True
    # holes are erased only within this balloon's own box, so the outline of an
    # overlapping neighbour (which looks like an enclosed hole) survives
    own = zone.copy()
    mx, my = int((a[2] - a[0]) * 0.1), int((a[3] - a[1]) * 0.1)
    own[max(0, a[1] - my):max(0, a[3] + my), max(0, a[0] - mx):max(0, a[2] + mx)] = True
    ink = dilate(~near, 3)
    erase = np.zeros((H, W), bool)
    erase[R[1]:R[3], R[0]:R[2]] = (dilate(holes, 5) & balloon & own) | (ink & closed & zone)

    full = balloon | (closed & zone)          # balloon including where the glyphs were
    mine = full & own
    if not mine.any():
        mine = full
    by, bx = np.nonzero(mine)
    # the balloon's width across the middle of the text → usable width; its height → height
    rows = np.nonzero(mine.any(axis=1))[0]
    mid = (t[1] + t[3]) // 2
    band = mine[max(0, mid - 6):mid + 6]
    cols = np.nonzero(band.all(axis=0))[0] if band.size else np.array([])
    if len(cols) > 10:
        x0, x1 = int(cols.min()), int(cols.max())
    else:
        x0, x1 = int(bx.min()), int(bx.max())
    box = [x0 + R[0], int(rows.min()) + R[1], x1 + R[0], int(rows.max()) + R[1]]
    # keep the lettering inside this balloon, not a neighbour it touches
    mx, my = (ab[2] - ab[0]) * 0.15, (ab[3] - ab[1]) * 0.15
    clip = [ab[0] - mx, ab[1] - my, ab[2] + mx, ab[3] + my]
    box = [max(box[0], clip[0]), max(box[1], clip[1]), min(box[2], clip[2]), min(box[3], clip[3])]
    if box[2] - box[0] < 10 or box[3] - box[1] < 10:
        box = list(ab)
    return bg, erase, box


def _flat_extent(arr: np.ndarray, box, bg, keep) -> list[int]:
    """Shrink `box` horizontally (never past `keep`) until its columns are plain background."""
    x0, y0, x1, y1 = [int(v) for v in box]
    col = np.asarray(bg, dtype=np.int16)
    def plain(x):
        strip = arr[y0:y1, x].astype(np.int16)
        return strip.size and np.mean(np.abs(strip - col).sum(axis=1) < 40) > 0.97
    while x0 < keep[0] and not plain(x0):
        x0 += 2
    while x1 - 1 > keep[2] and not plain(x1 - 1):
        x1 -= 2
    def plain_row(y):
        row = arr[y, x0:x1].astype(np.int16)
        return row.size and np.mean(np.abs(row - col).sum(axis=1) < 60) > 0.6
    # vertically too: don't spill into a black bar or the next panel above/below
    while y0 < keep[1] and not plain_row(y0):
        y0 += 1
    while y1 - 1 > keep[3] and not plain_row(y1 - 1):
        y1 -= 1
    return [x0, y0, x1, y1]


def plan_tile(tile: Image.Image, regions: list[dict]):
    """Erase source text; return the cleaned tile and where each English line goes."""
    im = tile.convert("RGB").copy()
    arr = np.asarray(im).copy()
    W, H = im.size
    plans = []
    for r in regions:
        if r.get("_noplace"):   # couldn't be located on the page (kept in the script only)
            continue
        words = _words(r["english"])
        glyph = r.get("glyph")   # height of the original letters (OCR), when known
        tb = _clamp(r["text_box"], W, H)
        ab = _clamp(r["area_box"], W, H)
        if not words or tb[2] - tb[0] < 4 or tb[3] - tb[1] < 4:
            continue
        if r["kind"] == "sfx":
            plans.append({"kind": "sfx", "words": words, "anchor": tb})
            continue
        oval = r["kind"] in ("speech", "thought")
        found = _find_balloon(arr, tb, ab)
        if found:
            bg, erase, box = found
            arr[erase] = bg
            if glyph:
                # centre on the original text (what the letterer did), keeping inside the balloon
                cx, cy = (tb[0] + tb[2]) / 2, (tb[1] + tb[3]) / 2
                hw = max((tb[2] - tb[0]) / 2, min(cx - box[0], box[2] - cx))
                hh = max((tb[3] - tb[1]) / 2, min(cy - box[1], box[3] - cy))
                box = [round(cx - hw), round(cy - hh), round(cx + hw), round(cy + hh)]
            ink = (0, 0, 0) if sum(bg) > 382 else (255, 255, 255)
            plans.append({"kind": r["kind"], "words": words, "box": box, "ink": ink, "oval": oval, "inset": True,
                          "glyph": glyph})
            continue
        eb = _clamp([tb[0] - 4, tb[1] - 4, tb[2] + 4, tb[3] + 4], W, H)
        ring = _ring(arr, eb, 8)
        bg, share = _dominant(ring) if ring.size else ((255, 255, 255), 0.0)
        narrow = False
        if glyph:
            # OCR + DeepL text outside a balloon: only replace it when it sits on a truly plain
            # background (narration on a blank page or a black bar). Anything near artwork —
            # titles, logos, signs, text over a drawing — is left exactly as drawn.
            wide = _ring(arr, _clamp([tb[0] - glyph, tb[1] - glyph // 2, tb[2] + glyph, tb[3] + glyph // 2], W, H),
                         max(8, glyph))
            _, wide_share = _dominant(wide) if wide.size else (bg, 0.0)
            if share < 0.9:
                continue            # text over a drawing: leave it
            narrow = wide_share < 0.95   # plain only right around the text (e.g. a balloon that
                                         # crosses a panel edge): letter inside, don't spread out
        if share > 0.55:
            # flat page or caption background without a closed outline
            # repaint only the rows that really are this background (a text box can overlap
            # the edge of a black bar or panel, which must not be painted over)
            col = np.asarray(bg, dtype=np.int16)
            for yy in range(eb[1], eb[3]):
                row = arr[yy, eb[0]:eb[2]].astype(np.int16)
                if row.size and np.mean(np.abs(row - col).sum(axis=1) < 60) >= 0.45:
                    arr[yy, eb[0]:eb[2]] = bg
            box = ab if (ab[2] - ab[0] > 8 and ab[3] - ab[1] > 8) else eb
            if glyph and not narrow:
                # open page/caption: English runs wider than Korean, so give it room around the
                # same centre (as official letterers do), as far as the flat background goes
                cx = (tb[0] + tb[2]) / 2
                half = max((tb[2] - tb[0]) / 2 + glyph, min(cx, W - cx) * 0.92)
                box = _clamp([cx - half, tb[1] - glyph * 1.5, cx + half, tb[3] + glyph * 1.5], W, H)
                box = _flat_extent(arr, box, bg, tb)
                for o in regions:   # never reach past halfway to another text block
                    if o is r or o.get("_noplace"):
                        continue
                    ob = o["text_box"]
                    if ob[0] < box[2] and ob[2] > box[0]:
                        if ob[1] >= tb[3]:
                            box[3] = min(box[3], round((tb[3] + ob[1]) / 2))
                        elif ob[3] <= tb[1]:
                            box[1] = max(box[1], round((ob[3] + tb[1]) / 2))
            ink = (0, 0, 0) if sum(bg) > 382 else (255, 255, 255)
            plans.append({"kind": r["kind"], "words": words, "box": box, "ink": ink, "oval": oval, "inset": True,
                          "glyph": glyph, "open": bool(glyph)})
        else:
            # text on busy art: cover with a clean caption box
            plans.append({"kind": r["kind"], "words": words, "box": eb, "ink": (0, 0, 0),
                          "oval": False, "inset": False, "caption": True})
    return Image.fromarray(arr), plans


def _inner(p):
    x0, y0, x1, y1 = p["box"]
    fx, fy = ((0.10, 0.10) if p["oval"] else (0.05, 0.06)) if p["inset"] else (0.04, 0.04)
    return [x0 + (x1 - x0) * fx, y0 + (y1 - y0) * fy, x1 - (x1 - x0) * fx, y1 - (y1 - y0) * fy]


def render_tile(im: Image.Image, plans, base_size: int) -> Image.Image:
    draw = ImageDraw.Draw(im)
    W, H = im.size
    lo = max(9, int(W * 0.016))
    for p in plans:
        words = p["words"]
        if p["kind"] == "sfx":
            size = max(lo, int(base_size * 0.8))
            text = " ".join("".join(t for t, _ in wd) for wd in words)
            f = _font(size, True)
            tw = f.getlength(text)
            tb = p["anchor"]
            cx = (tb[0] + tb[2]) / 2
            ty = min(H - size - 6, tb[3] + 2)
            bx = [max(2, cx - tw / 2 - 6), ty - 3, min(W - 2, cx + tw / 2 + 6), ty + size * 1.15 + 3]
            draw.rounded_rectangle(bx, radius=6, fill=(255, 255, 255), outline=(0, 0, 0), width=2)
            draw.text(((bx[0] + bx[2]) / 2 - tw / 2, ty), text, font=f, fill=(0, 0, 0))
            continue
        inner = _inner(p)
        iw, ih = inner[2] - inner[0], inner[3] - inner[1]
        if p.get("caption"):
            # grow the caption box until the text fits at a readable size
            size = max(lo, int(base_size * 0.85))
            while _wrap(words, size, iw, ih, False) is None and ih < H * 0.4:
                grow = max(6, ih * 0.15)
                p["box"] = _clamp([p["box"][0] - grow / 2, p["box"][1] - grow / 2,
                                   p["box"][2] + grow / 2, p["box"][3] + grow / 2], W, H)
                inner = _inner(p); iw, ih = inner[2] - inner[0], inner[3] - inner[1]
            draw.rounded_rectangle(p["box"], radius=8, fill=(255, 255, 255), outline=(0, 0, 0), width=2)
        hi = base_size
        if p.get("glyph"):
            # match the original lettering size: English caps about as tall as the Korean glyphs
            hi = max(lo, int(p["glyph"] * 0.95))
        size = _max_fit(words, iw, ih, p["oval"], hi, lo)
        lines = _wrap(words, size, iw, ih, p["oval"]) or _wrap(words, size, iw, ih, False) or [[w] for w in words]
        _draw_lines(draw, lines, size, inner, p["ink"])
    return im


def _episode_base_size(tiles, all_plans) -> int:
    """One dialogue size for the whole episode: the median of what each balloon could hold."""
    fits = []
    for tile, plans in zip(tiles, all_plans):
        hi, lo = int(tile.width * 0.05), max(9, int(tile.width * 0.016))
        for p in plans:
            if p["kind"] in ("speech", "thought", "narration") and not p.get("caption"):
                inner = _inner(p)
                w, h = inner[2] - inner[0], inner[3] - inner[1]
                if w > 10 and h > 10:
                    fits.append(_max_fit(p["words"], w, h, p["oval"], hi, lo))
    if not fits:
        return int(tiles[0].width * 0.038) if tiles else 24
    return int(statistics.median(fits))


# ---------------------------------------------------------------- 4. episode

def translate_episode(images: list[bytes], series_title: str, series_dir: str, episode_label: str,
                      strip: bool, on_progress=None, source_lang: str = "") -> tuple[list[tuple[str, bytes]], str]:
    """Returns ([(filename, jpeg bytes)], script markdown)."""
    if not available():
        raise TranslateError("Set DEEPL_API_KEY, GROQ_API_KEY or ANTHROPIC_API_KEY to use translation.")
    use_deepl = provider() == "deepl"
    if use_deepl or provider() == "groq":
        client = None
    else:
        import anthropic
        client = anthropic.Anthropic()

    gpath = os.path.join(series_dir, "glossary.json")
    try:
        with open(gpath, encoding="utf-8") as f:
            glossary = json.load(f)
    except (OSError, ValueError):
        glossary = {}

    tiles = make_tiles([Image.open(io.BytesIO(b)) for b in images], strip)
    per_tile: list[list[dict]] = [[] for _ in tiles]
    recent: list[str] = []
    refused = 0

    # cache: re-downloading the same episode (e.g. as another format) costs nothing
    import hashlib
    key = hashlib.sha1(b"".join(hashlib.sha1(b).digest() for b in images)).hexdigest()[:16]
    if provider() != "anthropic":   # each service keeps its own cached result
        key = provider() + "-" + key
    cpath = os.path.join(series_dir, ".translations", key + ".json")
    try:
        with open(cpath, encoding="utf-8") as f:
            cached = json.load(f)
        if len(cached) == len(tiles):
            per_tile = cached
    except (OSError, ValueError):
        cached = None
    per_req = TILES_PER_REQUEST if (use_deepl or client is not None) else GROQ_TILES_PER_REQUEST
    batches = range(0, len(tiles), per_req) if per_tile is not cached else range(0)
    for bi, start in enumerate(batches):
        if on_progress:
            on_progress(bi, len(batches))
        chunk = tiles[start:start + per_req]
        if use_deepl:
            data = _ask_deepl(chunk, start + 1, series_title, recent, source_lang)
        else:
            data = _ask(client, chunk, start + 1, series_title, glossary, recent, source_lang)
        refused += bool(data.get("_refused"))
        for t in data.get("tiles", []):
            idx = t["tile"] - 1
            if 0 <= idx < len(tiles):
                if client is None and not use_deepl:   # Groq: its boxes are unreliable, so snap them to OCR'd text lines
                    _snap_to_ocr(tiles[idx], t["regions"])
                per_tile[idx].extend(t["regions"])
                for r in t["regions"]:
                    recent.append(f"{r['original']} => {r['english']}")
        for g in data.get("glossary_additions", []):
            glossary.setdefault(g["source"], g["english"] + (f" ({g['note']})" if g.get("note") else ""))
    if on_progress:
        on_progress(len(batches), len(batches))

    os.makedirs(os.path.dirname(cpath), exist_ok=True)
    with open(gpath, "w", encoding="utf-8") as f:
        json.dump(glossary, f, ensure_ascii=False, indent=2)
    if not refused:
        with open(cpath, "w", encoding="utf-8") as f:
            json.dump(per_tile, f, ensure_ascii=False)

    if use_deepl:
        # titles and logos are drawn much bigger than dialogue: leave them as they are
        gl = sorted(r["glyph"] for rs in per_tile for r in rs if r.get("glyph"))
        if gl:
            med = gl[len(gl) // 2]
            for rs in per_tile:
                for r in rs:
                    if r.get("glyph") and r["glyph"] > med * 1.8:
                        r["_noplace"] = True
    planned = [plan_tile(t, r) for t, r in zip(tiles, per_tile)]
    base = _episode_base_size(tiles, [p for _, p in planned])
    out, script = [], [f"# {series_title} — {episode_label}\n"]
    if refused:
        script.append(f"> {refused} batch(es) were declined by the model and left untranslated.\n")
    for i, ((clean, plans), regions) in enumerate(zip(planned, per_tile), start=1):
        lettered = render_tile(clean, plans, base)
        buf = io.BytesIO()
        lettered.save(buf, "JPEG", quality=92)
        out.append((f"{i:03d}.jpg", buf.getvalue()))
        if regions:
            script.append(f"\n## Tile {i}\n")
            for r in regions:
                script.append(f"- *{r['kind']}* — {r['original']}  \n  → {r['english']}")
    return out, "\n".join(script) + "\n"
