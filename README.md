# Concert Lyrics

[![CI](https://github.com/shrutz510/concert-lyrics/actions/workflows/ci.yml/badge.svg)](https://github.com/shrutz510/concert-lyrics/actions/workflows/ci.yml)

Enter an artist's name. The app finds their latest setlist on setlist.fm and builds one page
with the lyrics to every song, which you can read at the concert.

**How to set it up and use it: [GUIDE.md](GUIDE.md)**

## Quick start

1. Get a free API key at https://www.setlist.fm/settings/api.
2. Copy `.env.example` to `.env` and paste your key after `SETLISTFM_API_KEY=`.
3. Run `python3 app.py` and open http://localhost:8765, or run `python3 app.py "Artist Name"`.

You only need Python 3. There's nothing else to install.

## Development

Run the tests (offline, no API key needed):

    python3 -m unittest discover -s tests

Lint and format with [ruff](https://docs.astral.sh/ruff/): `ruff check .` and `ruff format .`

**CI** runs on every push and pull request: lint, then tests on Python 3.10–3.13 (Linux) and macOS.
On `main` it also does a live run against setlist.fm, if the repository has a `SETLISTFM_API_KEY` secret.

**Releases:** bump `version` in `pyproject.toml`, then push a matching tag:

    git tag v1.0.1 && git push origin v1.0.1

CI runs again, then a GitHub Release is published with a downloadable zip of the app.
