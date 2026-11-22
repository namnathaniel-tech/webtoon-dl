# AGENTS.md

Webtoon Downloader: a local Python app that downloads episodes from comic.naver.com, webtoons.com and mangadex.org, reads them in a built-in reader, and can translate Korean episodes to English with Claude. See `README.md` for user-facing features.

## Dev environment
- Run the app with `./run.sh`. It creates `.venv`, installs `requirements.txt` (Pillow, certifi, anthropic, numpy, pywebview) and starts `app.py` on http://127.0.0.1:8765.
- Run Python through `.venv/bin/python`, not the system interpreter.
- Add new dependencies to `requirements.txt`; `run.sh` reinstalls when that file is newer than `.venv/.installed`.
- Translation picks a service by which key exists: DeepL (`DEEPL_API_KEY` or `~/Library/Application Support/webtoon-dl/deepl.key`; macOS Vision OCR finds the text, DeepL translates it), then Groq (`GROQ_API_KEY` or `.../groq.key`, model `qwen/qwen3.8-27b`, boxes snapped to OCR lines), then Claude (`ANTHROPIC_API_KEY`). Never hardcode or commit any key.

## Layout
- `app.py`: local HTTP server and API routes for the UI.
- `index.html`: the whole front end (library, reader, download UI) in one file.
- `webtoon_dl.py`: site scrapers, downloading, and the CLI (`--list`, `--start`, `--end`, `--format`, `--translate`).
- `translate.py`: Claude translation, text erasing and English lettering.
- `translation_guide.md`: the Korean translation and lettering rules sent to the model. Edit it to change translation behaviour.
- `library.py`: scans the save folder (`~/Downloads/Webtoons` by default).
- `accounts.py`, `login_window.py`: site logins via pywebview.
- `fonts/`, `icons/`: bundled assets. Comic Neue is used for lettering.

## Conventions
- Keep it dependency-light: standard library first, then the packages already in `requirements.txt`.
- Don't commit `.venv/`, `__pycache__/`, `.translations/` or downloaded episodes. `.gitignore` covers only the first two, so don't `git add` the rest.
- Translation results are cached in `.translations/`. Delete the cache entry when testing changes to translation or lettering.
- Never buy, unlock or bypass paid episodes. Signed-in users (accounts.py) may download episodes their own account has already unlocked; everything else is free episodes only.
- Translation must only reletter speech balloons and narration on plain backgrounds. Never paint over artwork, titles, logos or sound effects.

## Testing
- There is no test suite. Check changes by running `./run.sh` and trying the affected flow, or the CLI: `.venv/bin/python webtoon_dl.py URL --list`.
- For UI changes, load http://127.0.0.1:8765 and exercise the library and reader.

## Commits
- Keep commits small and focused, with a short imperative subject line.
