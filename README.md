# Concert Lyrics

Enter an artist's name. The app finds their latest setlist on setlist.fm and builds one page
with the lyrics to every song, which you can read at the concert.

**How to set it up and use it: [GUIDE.md](GUIDE.md)**

## Quick start

1. Get a free API key at https://www.setlist.fm/settings/api.
2. Copy `.env.example` to `.env` and paste your key after `SETLISTFM_API_KEY=`.
3. Run `python3 app.py` and open http://localhost:8765, or run `python3 app.py "Artist Name"`.

You only need Python 3. There's nothing else to install.
