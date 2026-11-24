# Concert Lyrics: User Guide

Enter an artist's name. The app finds their latest setlist on setlist.fm and builds one page
with the lyrics to every song, which you can read at the concert.

## Setup (one time)

1. Download the app:
   ```bash
   git clone https://github.com/shrutz510/concert-lyrics.git
   cd concert-lyrics
   ```
2. Get a free API key at <https://www.setlist.fm/settings/api>. A new key can take a few minutes to start working.
3. Copy `.env.example` to `.env` and paste your key after `SETLISTFM_API_KEY=`.

You only need Python 3. There's nothing else to install.

## Use

**Web app:** run `python3 app.py`, open <http://localhost:8765>, type an artist, and click **Get lyrics**.

**Command line:** run `python3 app.py "Artist Name"`.

Pages are saved in the `lyrics/` folder. Send one to your phone before the show so you can read it offline.

## If something goes wrong

- **Misspelled or partial name:** the app shows "Did you mean…?" choices instead of guessing.
- **No match at all:** it asks you to check the spelling.
- **Song with no lyrics available** (for example, unreleased): it stays in the list, marked "(no lyrics)".
- **Busy server:** the app retries automatically.
- **Key or internet problems:** the app tells you what to fix.

## Good to know

- The app uses the artist's most recent show, so tonight's songs may differ slightly.
- Lyrics come from [LRCLIB](https://lrclib.net). Setlists come from [setlist.fm](https://www.setlist.fm).
