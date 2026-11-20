# Webtoon Downloader

Download free episodes from **comic.naver.com** (Korean), **webtoons.com** (English etc.) and **mangadex.org** (manga, any translation language) for offline reading.

## Run the app

```sh
./run.sh
```

First run creates `.venv` with Pillow (PDF) and certifi (HTTPS). Opens http://127.0.0.1:8765.
Paste a series or episode link → pick episodes → choose Images / CBZ / PDF → Download.
Default save folder: `~/Downloads/Webtoons/<series>/`.

## CLI

```sh
.venv/bin/python webtoon_dl.py URL --list
.venv/bin/python webtoon_dl.py URL --start 1 --end 10 --format cbz -o ~/Downloads/Webtoons
```

## Limits

- Paid, locked, and login-only episodes (including adult titles) are skipped; it doesn't get around paywalls.
- MangaDex chapters that only link to an official site (e.g. MANGA Plus) show as locked. Use `--lang ko` etc. on the CLI, or the language menu in the app.
- For personal offline reading only — don't redistribute.
- If a site changes its page layout, the regexes in `webtoon_dl.py` (`_naver_images`, `_webtoons_images`) are what to update.
