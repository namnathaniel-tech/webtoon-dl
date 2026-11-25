# Webtoon Downloader

Download free episodes from **Naver Webtoon** (comic.naver.com, Korean), one-shot comics from **Naver Blog** (blog.naver.com), **WEBTOON** (webtoons.com, English and other languages) and **MangaDex** (manga in many languages), read them in a built-in reader, and optionally translate Korean episodes into English.

Runs on your own Mac in your browser. Nothing is uploaded anywhere.

## Install (Mac)

You need **Python 3.10 or newer**. If you're not sure, install the latest from [python.org/downloads](https://www.python.org/downloads/) (the macOS installer).

**Option A: with Terminal**

```sh
git clone https://github.com/namnathaniel-tech/webtoon-dl.git
cd webtoon-dl
./run.sh
```

**Option B: without Terminal**

1. On this page click **Code → Download ZIP**, then unzip it (for example into your Documents folder).
2. Open the folder and double-click **Webtoon Downloader.command**.
3. The first time, macOS may say it can't verify the file. Right-click it, choose **Open**, then **Open** again. You only need to do this once.

The first start takes a minute while it installs what it needs. Then the app opens at **http://127.0.0.1:8765**. Keep the Terminal window open while you use it; close it to quit.

Next time, run `./run.sh` from the `webtoon-dl` folder or double-click **Webtoon Downloader.command** again. (`./run.sh` only works from inside the folder; from anywhere else use the full path, e.g. `~/Documents/webtoon-dl/run.sh`.)

## Use it

1. Pick a site on the home page.
2. Paste a series or episode link from that site and click **Load**.
3. Tick the episodes you want (Naver Blog posts are single comics, so there's nothing to pick), choose **Images**, **CBZ** or **PDF**, and click **Download**.

Downloads go to `~/Downloads/Webtoons/<series>/` and show up under **Your library** on the home page, with the series' cover art and AniList info (other titles, genres, status, description).

### Reader

- **Scroll mode** joins the images into one seamless strip and attaches the next episode as you reach the end.
- **Pages mode** shows one page at a time (tap or click the sides, or use the arrow keys), with a left-to-right / right-to-left switch for manga.
- The top bar hides while you read; scroll up a little to bring it back. The thin bar on the left edge shows how far you are through the episode. Click or drag it to jump.
- It remembers where you stopped in each series and marks finished episodes.
- Keys: `N`/`P` next/previous episode, `M` mode, `W` width, `Space` page down, `Esc` back.

## Translate to English

Translation needs a free DeepL key. You set it up once, in the app's **Settings** page.

### Get a free DeepL key (about 2 minutes)

1. Open [deepl.com/pro-api](https://www.deepl.com/pro-api), pick **DeepL API Free**, then **Sign up for free**.
2. Create the account with your email. DeepL asks for a card to confirm you're a real person; the Free plan (500,000 characters a month) doesn't charge it.
3. Go to **Account → API Keys** ([deepl.com/your-account/keys](https://www.deepl.com/your-account/keys)) and copy your key. A Free key ends in `:fx`.

### Add it to the app

1. Start the app and click **⚙ Settings** at the top right of the home page.
2. Paste the key into the **DeepL** box and click **Save**. The app checks it with DeepL and shows **Saved** and how many characters you've used this month.
3. Open a series, tick **Translate to English**, and click **Download**.

The key is saved only on your Mac (`~/Library/Application Support/webtoon-dl/`, readable only by your account). To change or remove it, go back to Settings.

### How it works

macOS text recognition finds the text in each speech balloon, DeepL translates it, and the app erases the Korean and letters the English in the same balloon. Sound effects, titles and anything drawn over artwork are left as drawn. A script (original → English) is saved next to the episode.

It's machine translation and machine lettering, so expect plain wording and the odd misplaced line, especially on pages full of small on-screen text.

### Other services (optional)

Settings also takes a **Groq** key ([console.groq.com/keys](https://console.groq.com/keys)), used only when there's no DeepL key. It uses an AI model that follows the rules in `translation_guide.md`, but on Groq's free plan it's slow (about a page a minute). Advanced users can set `ANTHROPIC_API_KEY` in the terminal to use Claude instead. The app uses DeepL first, then Groq, then Claude.

## Signing in (optional)

On the Naver or WEBTOON page, **Sign in** opens that site's own login page in a small window. The app never sees your password; it only keeps the site's login cookies, privately on your Mac. **Sign out** deletes them. Signed in, you can download episodes your account has already unlocked and age-restricted titles your account can open. The app never buys or unlocks anything.

## Command line

```sh
.venv/bin/python webtoon_dl.py URL --list
.venv/bin/python webtoon_dl.py URL --start 1 --end 10 --format cbz -o ~/Downloads/Webtoons
.venv/bin/python webtoon_dl.py URL --start 1 --end 1 --translate
```

## Troubleshooting

- **"zsh: no such file or directory: ./run.sh"**: you're not in the app's folder. `cd` into it first, or use the full path to `run.sh`.
- **"Port 8765 is already in use"**: the app is already running; it opens that one. To run a second copy: `WEBTOON_PORT=8766 ./run.sh`.
- **A download seems stuck**: if the Terminal window was closed or the app restarted, the page tells you; click **Download** again.
- **"Needs Python 3.10 or newer"**: install Python from python.org, then run again.

## Limits

- Paid and locked episodes download only if your signed-in account has already unlocked them; this doesn't get around paywalls.
- MangaDex chapters that only link to an official site (e.g. MANGA Plus) show as locked.
- Translation needs macOS (it uses the built-in text recognition).
- For personal offline reading only. Don't redistribute downloaded or translated episodes.
- If a site changes its page layout, the parsers in `webtoon_dl.py` (`_naver_images`, `_webtoons_images`) are what to update.
