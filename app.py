#!/usr/bin/env python3
"""Concert Lyrics: build one page with the lyrics to every song in an
artist's latest setlist.

  python3 app.py                 # web app at http://localhost:8765
  python3 app.py "Artist Name"   # command line

Setlists come from setlist.fm (needs a free key in .env as SETLISTFM_API_KEY),
lyrics from LRCLIB, and spelling suggestions from MusicBrainz.
Standard library only.
"""

import difflib
import html
import itertools
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "lyrics"
TEMPLATES = HERE / "templates"
PORT = int(os.environ.get("PORT", "8765"))
USER_AGENT = "ConcertLyrics/1.0 (personal setlist lyrics tool)"
OLD_SETLIST_DAYS = 90


class AppError(Exception):
    """A problem the user can fix. The message is shown to them as-is."""


class NeedsChoice(AppError):
    """The name wasn't an exact match. `suggestions` lists artists to pick from."""

    def __init__(self, message, suggestions):
        super().__init__(message)
        self.suggestions = suggestions


# ---------------------------------------------------------------- helpers


def load_api_key():
    key = os.environ.get("SETLISTFM_API_KEY", "")
    env = HERE / ".env"
    if not key and env.exists():
        for line in env.read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "SETLISTFM_API_KEY":
                key = value.strip(" \"'")
    return key.strip()


def ascii_fold(text):
    """'Beyoncé' -> 'Beyonce'."""
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()


def norm(text):
    """Comparison key that ignores case, accents, punctuation, a leading 'The' and '&' vs 'and'."""
    text = re.sub(r"^the\s+", "", ascii_fold(text).lower().strip()).replace("&", "and")
    return re.sub(r"[^a-z0-9]", "", text)


def similarity(query, name):
    q, n = norm(query), norm(name)
    if not q or not n:
        return 0.0
    score = difflib.SequenceMatcher(None, q, n).ratio()
    return max(score, 0.7) if q in n else score  # a partial name still counts as close


def get_json(url, headers=None, tries=4):
    """GET JSON, retrying when the server is busy or the network blips. None on 404."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code not in (429, 500, 502, 503, 504) or attempt == tries:
                raise
        except (OSError, ValueError):  # network error, timeout, or a garbled reply
            if attempt == tries:
                raise
        time.sleep(1.5 * attempt)


def query_url(base, **params):
    return base + "?" + urllib.parse.urlencode(params)


# ---------------------------------------------------------------- setlist.fm


def setlistfm(path, api_key, **params):
    if not api_key:
        raise AppError(
            "No setlist.fm API key found. Get a free one at "
            "https://www.setlist.fm/settings/api and put it in a .env file in "
            "this folder as SETLISTFM_API_KEY=your-key"
        )
    url = query_url(f"https://api.setlist.fm/rest/1.0/{path}", **params)
    try:
        return get_json(url, {"x-api-key": api_key, "Accept": "application/json"}) or {}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AppError(
                "setlist.fm rejected the API key. Check SETLISTFM_API_KEY in .env "
                "(a brand-new key can take a few minutes to start working)."
            ) from None
        if e.code == 429:
            raise AppError("setlist.fm is rate-limiting requests. Wait a minute and try again.") from None
        raise AppError(f"setlist.fm returned an error ({e.code}). Try again in a moment.") from None
    except OSError:
        raise AppError("Couldn't reach setlist.fm. Check your internet connection.") from None


def artist_entry(name, mbid, note=""):
    return {"name": name, "mbid": mbid, "note": note or ""}


def musicbrainz_suggestions(name):
    """Spelling-tolerant artist search. Best effort: any failure just means no extra ideas."""
    words = [w for w in re.findall(r"[\w']+", name) if len(w) > 1]
    if not words:
        return []
    try:
        data = (
            get_json(
                query_url(
                    "https://musicbrainz.org/ws/2/artist",
                    fmt="json",
                    limit=8,
                    query=" OR ".join(w + "~" for w in words),
                )
            )
            or {}
        )
    except Exception:
        return []
    return [artist_entry(a["name"], a["id"], a.get("disambiguation")) for a in data.get("artists", []) if a.get("id")]


def find_artist(name, api_key):
    """The artist whose name matches exactly. Otherwise ask the user rather than guess."""
    data = setlistfm("search/artists", api_key, artistName=name, sort="relevance")
    found = [
        artist_entry(a["name"], a["mbid"], a.get("disambiguation")) for a in data.get("artist", []) if a.get("mbid")
    ]
    exact = [a for a in found if norm(a["name"]) == norm(name)]
    if len(exact) > 1:  # same name, different artists: keep the ones that actually play shows
        exact = [a for a in exact if latest_setlist(a["mbid"], api_key, pages=1)] or exact
    if len(exact) == 1:
        return exact[0]
    if exact:
        others = [a for a in found if a not in exact]
        raise NeedsChoice(f'Several artists match "{name}". Which one?', (exact + others)[:8])

    candidates = {a["mbid"]: a for a in musicbrainz_suggestions(name) + found}.values()
    close = sorted(
        (a for a in candidates if similarity(name, a["name"]) >= 0.6),
        key=lambda a: similarity(name, a["name"]),
        reverse=True,
    )
    if close:
        raise NeedsChoice(f'No artist called "{name}" on setlist.fm. Did you mean one of these?', close[:6])
    raise AppError(f'Couldn\'t find an artist called "{name}". Check the spelling and try again.')


def songs_in(setlist):
    """Songs in play order, skipping intro/outro music played from tape."""
    songs = []
    for part in setlist.get("sets", {}).get("set", []):
        section = part.get("name") or (f"Encore {part['encore']}" if part.get("encore") else "")
        for s in part.get("song", []):
            if s.get("tape") or not s.get("name"):
                continue
            cover = (s.get("cover") or {}).get("name")
            songs.append(
                {
                    "title": s["name"],
                    "section": section.rstrip(":"),
                    "artist": cover or setlist["artist"]["name"],
                    "is_cover": bool(cover),
                    "with": (s.get("with") or {}).get("name"),
                    "info": s.get("info", ""),
                }
            )
    return songs


def latest_setlist(mbid, api_key, pages=3):
    """Newest setlist that has songs listed (announced shows have none yet)."""
    for page in range(1, pages + 1):
        setlists = setlistfm(f"artist/{mbid}/setlists", api_key, p=page).get("setlist", [])
        for s in setlists:
            if songs_in(s):
                return s
        if len(setlists) < 20:  # that was the last page
            return None
    return None


# ---------------------------------------------------------------- lyrics


def fetch_lyrics(title, artist):
    """Returns (lyrics, None) or (None, reason they're missing)."""
    try:
        results = (
            get_json(query_url("https://lrclib.net/api/search", track_name=title, artist_name=artist))
            or get_json(query_url("https://lrclib.net/api/search", track_name=title))
            or []
        )
    except Exception:
        return None, "the lyrics site didn't respond"

    want_artist, want_title = norm(artist), norm(title)
    for r in sorted(
        results,
        reverse=True,
        key=lambda r: (
            norm(r.get("artistName")) == want_artist,
            norm(r.get("trackName")) == want_title,
            bool(r.get("plainLyrics")),
        ),
    ):
        got_artist = norm(r.get("artistName"))
        if not (got_artist and (want_artist in got_artist or got_artist in want_artist)):
            continue  # never use a different artist's song that happens to share the title
        if r.get("instrumental"):
            return None, "instrumental"
        if r.get("plainLyrics"):
            return r["plainLyrics"].strip(), None
    return None, "not in the lyrics database (it may be unreleased)"


# ---------------------------------------------------------------- the lyrics book


def build_book(name, api_key, mbid=None):
    """Find the artist (unless `mbid` is already known), their latest setlist, and all lyrics."""
    name = (name or "").strip()
    if mbid:
        if not re.fullmatch(r"[0-9a-f-]{36}", mbid):
            raise AppError("Invalid artist id.")
    elif not name:
        raise AppError("Enter an artist name.")
    elif len(name) > 100:
        raise AppError("That artist name is too long.")
    else:
        mbid = find_artist(name, api_key)["mbid"]

    setlist = latest_setlist(mbid, api_key)
    if not setlist:
        raise AppError(
            f'"{name}" is on setlist.fm, but none of their shows has a song list yet. '
            "Try again after their next concert is posted."
        )
    songs = songs_in(setlist)
    with ThreadPoolExecutor(max_workers=4) as pool:
        for song, (lyrics, missing) in zip(songs, pool.map(lambda s: fetch_lyrics(s["title"], s["artist"]), songs)):
            song["lyrics"], song["missing"] = lyrics, missing

    venue = setlist.get("venue", {})
    city = venue.get("city", {})
    return {
        "artist": setlist["artist"]["name"],
        "date": datetime.strptime(setlist["eventDate"], "%d-%m-%Y").date(),
        "venue": venue.get("name", ""),
        "place": ", ".join(filter(None, [city.get("name"), city.get("country", {}).get("name")])),
        "tour": (setlist.get("tour") or {}).get("name", ""),
        "url": setlist.get("url", ""),
        "songs": songs,
    }


def old_warning(book):
    age = (date.today() - book["date"]).days
    if age > OLD_SETLIST_DAYS:
        return f"This is their most recent setlist, but it's {age} days old. Tonight's songs may be quite different."
    return ""


def nice_date(d):
    return f"{d:%B} {d.day}, {d.year}"


def render_book(book):
    e = html.escape
    songs = book["songs"]
    numbered = list(enumerate(songs, 1))

    song_list = []
    for section, group in itertools.groupby(numbered, key=lambda pair: pair[1]["section"]):
        group = list(group)
        if section:
            song_list.append(f'<div class="sec">{e(section)}</div>')
        items = "".join(
            f'<li><a href="#s{i}">{e(s["title"])}</a>'
            f"{'' if s['lyrics'] else ' <span class=mark>(no lyrics)</span>'}</li>"
            for i, s in group
        )
        song_list.append(f'<ol start="{group[0][0]}">{items}</ol>')

    sections = []
    for i, s in numbered:
        byline = [f"Cover of {e(s['artist'])}" if s["is_cover"] else e(s["artist"])]
        if s["with"]:
            byline.append(f"with {e(s['with'])}")
        if s["info"]:
            byline.append(e(s["info"]))
        body = (
            f'<div class="lyrics">{e(s["lyrics"])}</div>'
            if s["lyrics"]
            else f'<p class="missing">Lyrics not available: {e(s["missing"])}.</p>'
        )
        sections.append(
            f'<section class="song" id="s{i}"><h2>{i}. {e(s["title"])}</h2>'
            f'<div class="by">{" · ".join(byline)}</div>{body}</section>'
        )

    found = sum(1 for s in songs if s["lyrics"])
    meta = [
        f"Latest setlist: {nice_date(book['date'])}",
        e(" · ".join(filter(None, [book["venue"], book["place"]]))),
        e(book["tour"]),
        f"{len(songs)} songs · lyrics for {found}"
        + (f' · <a href="{e(book["url"])}">setlist.fm</a>' if book["url"] else ""),
    ]
    warning = old_warning(book)

    return Template((TEMPLATES / "book.html").read_text()).substitute(
        artist=e(book["artist"]),
        meta="<br>".join(filter(None, meta)),
        warning=f'<div class="warn">{e(warning)}</div>' if warning else "",
        song_list="".join(song_list),
        songs="".join(sections),
    )


def save_book(book):
    OUT_DIR.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_fold(book["artist"]).lower()).strip("-") or "concert"
    path = OUT_DIR / f"{slug}-{book['date'].isoformat()}.html"
    path.write_text(render_book(book), encoding="utf-8")
    return path


# ---------------------------------------------------------------- web app

KEY_WARNING = """<div class="warn">No setlist.fm API key set yet. Get a free one at
<a href="https://www.setlist.fm/settings/api" target="_blank">setlist.fm/settings/api</a>, then put
<code>SETLISTFM_API_KEY=your-key</code> in a <code>.env</code> file in this folder and restart.</div>"""


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body, content_type="text/html; charset=utf-8"):
        if not isinstance(body, str):
            body, content_type = json.dumps(body), "application/json"
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        params = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}

        if url.path == "/":
            page = Template((TEMPLATES / "home.html").read_text())
            self.reply(200, page.substitute(key_warning="" if load_api_key() else KEY_WARNING))
        elif url.path == "/build":
            try:
                path = save_book(build_book(params.get("artist"), load_api_key(), params.get("mbid")))
                self.reply(200, {"url": "/lyrics/" + urllib.parse.quote(path.name)})
            except NeedsChoice as e:
                self.reply(409, {"error": str(e), "suggestions": e.suggestions})
            except AppError as e:
                self.reply(400, {"error": str(e)})
            except Exception as e:
                self.reply(500, {"error": f"Something unexpected went wrong ({type(e).__name__}). Try again."})
        elif url.path.startswith("/lyrics/"):
            file = OUT_DIR / Path(urllib.parse.unquote(url.path)).name  # .name blocks ../ tricks
            if file.is_file():
                self.reply(200, file.read_text(encoding="utf-8"))
            else:
                self.reply(404, "Not found", "text/plain")
        else:
            self.reply(404, "Not found", "text/plain")

    def log_message(self, *args):
        pass


def run_server():
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        sys.exit(
            f"Port {PORT} is already in use. Is the app already running? "
            f"Open http://localhost:{PORT} or run with PORT=8766 python3 app.py"
        )
    print(f"Concert Lyrics running at http://localhost:{PORT}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


# ---------------------------------------------------------------- command line


def ask_which(choice):
    """Show the suggestions and return the picked artist, or exit."""
    print(choice)
    for i, s in enumerate(choice.suggestions, 1):
        print(f"  {i}. {s['name']}" + (f"  ({s['note']})" if s["note"] else ""))
    if not sys.stdin.isatty():
        sys.exit(2)
    pick = input("Pick a number (or press Enter to cancel): ").strip()
    if not (pick.isdigit() and 1 <= int(pick) <= len(choice.suggestions)):
        sys.exit("Cancelled.")
    return choice.suggestions[int(pick) - 1]


def run_cli(name):
    key = load_api_key()
    try:
        try:
            book = build_book(name, key)
        except NeedsChoice as choice:
            picked = ask_which(choice)
            book = build_book(picked["name"], key, picked["mbid"])
    except AppError as e:
        sys.exit(f"Error: {e}")

    path = save_book(book)
    print(f"{book['artist']}: {nice_date(book['date'])} at {book['venue']}")
    print(f"{len(book['songs'])} songs, lyrics found for {sum(1 for s in book['songs'] if s['lyrics'])}")
    for s in book["songs"]:
        if not s["lyrics"]:
            print(f"  no lyrics: {s['title']} ({s['missing']})")
    if old_warning(book):
        print("Note: " + old_warning(book))
    print(f"Saved: {path}")


if __name__ == "__main__":
    try:
        run_cli(" ".join(sys.argv[1:])) if len(sys.argv) > 1 else run_server()
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
