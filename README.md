# Webtoon Downloader

Download free episodes from **comic.naver.com** (Korean), **webtoons.com** (English etc.) and **mangadex.org** (manga, any translation language) for offline reading.

## Run the app

```sh
./run.sh
```

First run creates `.venv` with Pillow (PDF) and certifi (HTTPS). Opens http://127.0.0.1:8765.
Paste a series or episode link → pick episodes → choose Images / CBZ / PDF → Download.
Default save folder: `~/Downloads/Webtoons/<series>/`.

## Reader

Everything you download shows up under **Your library** on the home page (the save folder, `~/Downloads/Webtoons` by default).

- **Scroll mode** joins the images into one seamless strip and attaches the next episode as you reach the end, so reading never stops.
- **Pages mode** shows one page at a time (tap or click the left/right side, or use the arrow keys), with a left-to-right or right-to-left switch for manga. Each series picks scroll or pages from the shape of its pages, and remembers your choice.
- **EN / Original** switches between the translated and original version when both exist.
- **Width** cycles 600 / 760 / 960 px / full width.
- It remembers where you stopped in each series ("Continue: …") and marks episodes you've finished.
- Keys: `N`/`P` next/previous episode, `M` mode, `W` width, `Space` page down, `Esc` back.

## CLI

```sh
.venv/bin/python webtoon_dl.py URL --list
.venv/bin/python webtoon_dl.py URL --start 1 --end 10 --format cbz -o ~/Downloads/Webtoons
```

## Translate to English

Tick **Translate to English** before downloading (CLI: `--translate`). Uses DeepL when a DeepL key is set in `DEEPL_API_KEY` or saved in `~/Library/Application Support/webtoon-dl/deepl.key`: macOS text recognition (Vision) finds each balloon's text and DeepL translates it; sound effects aren't translated and the translation guide isn't used. Otherwise it uses Groq (`qwen/qwen3.8-27b`) when a Groq key is set in `GROQ_API_KEY` or saved in `~/Library/Application Support/webtoon-dl/groq.key`; otherwise Claude with `ANTHROPIC_API_KEY`. Keys are never stored in the repo.

- Claude (Opus 5.5) reads the episode in slices, cut at the blank gaps between panels, and translates every balloon, caption and sound effect using `translation_guide.md`, a condensed version of the Korean translation and lettering notes.
- The app erases the original text and letters the English in all caps with Comic Neue (open font licence), shaped to fit the balloon, at one size per episode, with bold italic for emphasis. Sound effects get a small English label next to the art instead of being erased.
- Saved next to the pages: `<episode> [EN] - script.md` (original → English, line by line) and `glossary.json` (names and terms, reused for later episodes of the same series).
- Results are cached in `.translations/`, so downloading the same episode again (for example as CBZ) doesn't call the API again.
- Cost: roughly $1 per Naver episode. It's machine lettering, so expect the odd leftover Korean stroke where text touches a balloon outline.

## Limits

- Paid, locked, and login-only episodes (including adult titles) are skipped; it doesn't get around paywalls.
- MangaDex chapters that only link to an official site (e.g. MANGA Plus) show as locked. Use `--lang ko` etc. on the CLI, or the language menu in the app.
- For personal offline reading only — don't redistribute.
- If a site changes its page layout, the regexes in `webtoon_dl.py` (`_naver_images`, `_webtoons_images`) are what to update.
