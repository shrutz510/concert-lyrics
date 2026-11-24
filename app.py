#!/usr/bin/env python3
"""Concert Lyrics: get an artist's latest setlist from setlist.fm, fetch the
lyrics for every song, and build one phone-friendly lyrics book.

Usage:
  python3 app.py                 # start the web app at http://localhost:8765
  python3 app.py "Artist Name"   # build the lyrics book from the command line

Needs a free setlist.fm API key (https://www.setlist.fm/settings/api), supplied
as SETLISTFM_API_KEY in the environment or in a .env file next to this script.
Lyrics come from LRCLIB (https://lrclib.net); spelling suggestions use
MusicBrainz (https://musicbrainz.org). Neither needs a key.
Only the Python standard library is used.
"""

import difflib
import html
import json
import os
import re
import socket
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

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "lyrics"
PORT = int(os.environ.get("PORT", "8765"))
USER_AGENT = "ConcertLyrics/1.0 (personal setlist lyrics tool)"
SETLIST_API = "https://api.setlist.fm/rest/1.0"
OLD_SETLIST_DAYS = 90  # warn when the latest setlist is older than this


# ---------------------------------------------------------------- errors

class AppError(Exception):
    """A problem the user can act on; the message is shown as-is."""


class NeedsChoice(AppError):
    """The artist name didn't match exactly; offer these artists instead."""

    def __init__(self, message, suggestions):
        super().__init__(message)
        self.suggestions = suggestions  # [{"name", "mbid", "note"}]


# ---------------------------------------------------------------- config

def load_api_key():
    key = os.environ.get("SETLISTFM_API_KEY", "").strip()
    env = HERE / ".env"
    if not key and env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith("SETLISTFM_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    return key


# ---------------------------------------------------------------- http

def get_json(url, headers=None, tries=4):
    """GET a JSON URL. Returns None on 404. Retries busy/rate-limit/network errors."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, socket.timeout, ConnectionError, json.JSONDecodeError):
            if attempt < tries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise


def setlistfm(path, params, api_key):
    if not api_key:
        raise AppError(
            "No setlist.fm API key found. Get a free one at "
            "https://www.setlist.fm/settings/api and put it in a .env file in this "
            "folder as SETLISTFM_API_KEY=your-key")
    url = f"{SETLIST_API}/{path}?" + urllib.parse.urlencode(params)
    try:
        return get_json(url, {"x-api-key": api_key, "Accept": "application/json"})
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AppError("setlist.fm rejected the API key. Check SETLISTFM_API_KEY in "
                           ".env (a brand-new key can take a few minutes to start working).")
        if e.code == 429:
            raise AppError("setlist.fm is rate-limiting requests. Wait a minute and try again.")
        raise AppError(f"setlist.fm returned an error ({e.code}). Try again in a moment.")
    except (urllib.error.URLError, socket.timeout, ConnectionError):
        raise AppError("Couldn't reach setlist.fm. Check your internet connection.")


# ---------------------------------------------------------------- artist lookup

def _norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"^the\s+", "", s.lower().strip())
    s = s.replace("&", "and")
    return re.sub(r"[^a-z0-9]", "", s)


def _similarity(query, name):
    q, n = _norm(query), _norm(name)
    if not q or not n:
        return 0.0
    r = difflib.SequenceMatcher(None, q, n).ratio()
    if q in n:  # partial name, e.g. first name only
        r = max(r, 0.7)
    return r


def musicbrainz_suggestions(name):
    """Spelling-tolerant artist search. Best effort: returns [] on any failure."""
    words = [w for w in re.findall(r"[\w']+", name) if len(w) > 1]
    if not words:
        return []
    query = " OR ".join(f"{w}~" for w in words)
    try:
        data = get_json("https://musicbrainz.org/ws/2/artist?" + urllib.parse.urlencode(
            {"query": query, "fmt": "json", "limit": 8})) or {}
    except Exception:
        return []
    return [{"name": a["name"], "mbid": a["id"], "note": a.get("disambiguation", "")}
            for a in data.get("artists", []) if a.get("id")]


def resolve_artist(name, api_key):
    """Return {"name", "mbid"} for an exact match, else raise NeedsChoice/AppError."""
    data = setlistfm("search/artists", {"artistName": name, "sort": "relevance"}, api_key) or {}
    found = [{"name": a["name"], "mbid": a["mbid"], "note": a.get("disambiguation", "")}
             for a in data.get("artist", []) if a.get("mbid")]
    exact = [a for a in found if _norm(a["name"]) == _norm(name)]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        # Several artists share this name: use the one that has played shows.
        with_shows = [a for a in exact if latest_setlist(a["mbid"], api_key, pages=1)]
        if len(with_shows) == 1:
            return with_shows[0]
        others = [a for a in found if a not in exact][:6]
        raise NeedsChoice(f'Several artists match "{name}". Which one?',
                          ((with_shows or exact) + others)[:8])

    # No exact match: never guess. Build "did you mean" suggestions.
    pool = {a["mbid"]: a for a in musicbrainz_suggestions(name)}
    pool.update({a["mbid"]: a for a in found})
    ranked = sorted(pool.values(), key=lambda a: _similarity(name, a["name"]), reverse=True)
    suggestions = [a for a in ranked if _similarity(name, a["name"]) >= 0.6][:6]
    if suggestions:
        raise NeedsChoice(f'No artist called "{name}" on setlist.fm. Did you mean one of these?',
                          suggestions)
    raise AppError(f'Couldn\'t find an artist called "{name}". Check the spelling and try again.')


# ---------------------------------------------------------------- setlists

def latest_setlist(mbid, api_key, pages=3):
    """Newest setlist for this artist that has songs listed (skips announced/empty shows)."""
    for p in range(1, pages + 1):
        data = setlistfm(f"artist/{mbid}/setlists", {"p": p}, api_key)
        items = (data or {}).get("setlist", [])
        for s in items:
            if songs_from(s):
                return s
        if len(items) < 20:
            break
    return None


def songs_from(setlist):
    songs = []
    for part in setlist.get("sets", {}).get("set", []):
        section = (part.get("name") or (f"Encore {part['encore']}" if part.get("encore") else "")).rstrip(":")
        for s in part.get("song", []):
            if s.get("tape") or not s.get("name"):
                continue  # intro/outro music played from tape
            songs.append({
                "title": s["name"],
                "section": section,
                "artist": (s.get("cover") or {}).get("name") or setlist["artist"]["name"],
                "is_cover": bool(s.get("cover")),
                "with": (s.get("with") or {}).get("name"),
                "info": s.get("info", ""),
            })
    return songs


# ---------------------------------------------------------------- lyrics

def fetch_lyrics(title, artist):
    """Look the song up on LRCLIB. Returns (lyrics or None, status)."""
    try:
        res = get_json("https://lrclib.net/api/search?" + urllib.parse.urlencode(
            {"track_name": title, "artist_name": artist})) or []
        if not res:  # retry with the title alone, e.g. for odd artist spellings
            res = get_json("https://lrclib.net/api/search?" + urllib.parse.urlencode(
                {"track_name": title})) or []
    except Exception:
        return None, "the lyrics site didn't respond"
    t, a = _norm(title), _norm(artist)

    def score(r):
        return ((_norm(r.get("artistName", "")) == a) * 4
                + (_norm(r.get("trackName", "")) == t) * 2
                + bool(r.get("plainLyrics")))

    for r in sorted(res, key=score, reverse=True):
        ra = _norm(r.get("artistName", ""))
        if not (a and ra and (a in ra or ra in a)):
            break  # never use a different artist's song with the same title
        if r.get("instrumental"):
            return None, "instrumental"
        if r.get("plainLyrics"):
            return r["plainLyrics"].strip(), "ok"
    return None, "not in the lyrics database (it may be unreleased)"


# ---------------------------------------------------------------- book

def build_book(artist_name, api_key, mbid=None):
    artist_name = (artist_name or "").strip()
    if not mbid:
        if not artist_name:
            raise AppError("Enter an artist name.")
        if len(artist_name) > 100:
            raise AppError("That artist name is too long.")
        artist = resolve_artist(artist_name, api_key)
        artist_name, mbid = artist["name"], artist["mbid"]
    elif not re.fullmatch(r"[0-9a-f-]{36}", mbid):
        raise AppError("Invalid artist id.")

    sl = latest_setlist(mbid, api_key)
    if not sl:
        raise AppError(f'"{artist_name}" is on setlist.fm, but none of their shows has a '
                       "song list yet. Try again after their next concert is posted.")
    songs = songs_from(sl)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda s: fetch_lyrics(s["title"], s["artist"]), songs))
    for s, (lyr, status) in zip(songs, results):
        s["lyrics"], s["status"] = lyr, status

    when = datetime.strptime(sl["eventDate"], "%d-%m-%Y").date()
    age = (date.today() - when).days
    venue = sl.get("venue", {})
    city = venue.get("city", {})
    return {
        "artist": sl["artist"]["name"],
        "iso_date": when.isoformat(),
        "date": f"{when:%B} {when.day}, {when.year}",
        "old_warning": (f"This is their most recent setlist, but it's {age} days old. "
                        "Tonight's songs may be quite different.") if age > OLD_SETLIST_DAYS else "",
        "venue": venue.get("name", ""),
        "place": ", ".join(x for x in (city.get("name"), city.get("country", {}).get("name")) if x),
        "tour": (sl.get("tour") or {}).get("name", ""),
        "url": sl.get("url", ""),
        "songs": songs,
    }


# ---------------------------------------------------------------- rendering

BOOK_CSS = """
:root{--bg:#fbf8f3;--fg:#1d1b19;--muted:#6f6860;--line:#e4ddd2;--accent:#8a3b2e;--card:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#121110;--fg:#ece6dd;--muted:#9a9187;--line:#2c2926;--accent:#e08a6f;--card:#1b1a18}}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--fg);font:17px/1.6 Georgia,"Iowan Old Style",serif}
main{max-width:640px;margin:0 auto;padding:24px 16px 80px}
header h1{font-size:2rem;margin:0 0 4px;line-height:1.2}
.meta{color:var(--muted);font:14px/1.5 -apple-system,system-ui,sans-serif}
.meta a{color:var(--accent)}
.warn{margin-top:10px;padding:10px 12px;border:1px solid var(--line);border-radius:8px;font:14px/1.5 -apple-system,system-ui,sans-serif}
nav ol{padding-left:1.6em;font:15px/1.9 -apple-system,system-ui,sans-serif}
nav a{color:var(--fg);text-decoration:none;border-bottom:1px solid var(--line)}
.sec{font:600 12px/1 -apple-system,system-ui,sans-serif;letter-spacing:.1em;text-transform:uppercase;color:var(--accent);margin:18px 0 4px}
.song{border-top:1px solid var(--line);padding-top:28px;margin-top:36px;scroll-margin-top:12px}
.song h2{margin:0;font-size:1.45rem;line-height:1.25}
.song .by{color:var(--muted);font:14px/1.5 -apple-system,system-ui,sans-serif;margin:4px 0 16px}
.lyrics{white-space:pre-wrap;font-size:1.06rem;line-height:1.7}
.missing{color:var(--muted);font-style:italic}
.top{position:fixed;right:16px;bottom:16px;background:var(--accent);color:#fff;border-radius:999px;
  padding:10px 16px;font:600 14px/1 -apple-system,system-ui,sans-serif;text-decoration:none;box-shadow:0 2px 8px #0003}
.mark{color:var(--muted);font-size:12px}
"""


def render_book(book):
    e = html.escape
    toc, body, last_sec = [], [], None
    for i, s in enumerate(book["songs"], 1):
        if s["section"] != last_sec and s["section"]:
            toc.append(f'</ol><div class="sec">{e(s["section"])}</div><ol start="{i}">')
        last_sec = s["section"]
        flag = "" if s["lyrics"] else ' <span class="mark">(no lyrics)</span>'
        toc.append(f'<li><a href="#s{i}">{e(s["title"])}</a>{flag}</li>')
        by = [f'Cover of {e(s["artist"])}' if s["is_cover"] else e(s["artist"])]
        if s["with"]:
            by.append(f'with {e(s["with"])}')
        if s["info"]:
            by.append(e(s["info"]))
        lyr = (f'<div class="lyrics">{e(s["lyrics"])}</div>' if s["lyrics"] else
               f'<p class="missing">Lyrics not available: {e(s["status"])}.</p>')
        body.append(f'<section class="song" id="s{i}"><h2>{i}. {e(s["title"])}</h2>'
                    f'<div class="by">{" · ".join(by)}</div>{lyr}</section>')
    found = sum(1 for s in book["songs"] if s["lyrics"])
    where = " · ".join(x for x in (book["venue"], book["place"]) if x)
    tour = f'<br>{e(book["tour"])}' if book["tour"] else ""
    src = f' · <a href="{e(book["url"])}">setlist.fm</a>' if book["url"] else ""
    warn = f'<div class="warn">{e(book["old_warning"])}</div>' if book["old_warning"] else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(book["artist"])} Lyrics</title><style>{BOOK_CSS}</style></head>
<body><main id="top"><header><h1>{e(book["artist"])}</h1>
<div class="meta">Latest setlist: {e(book["date"])}<br>{e(where)}{tour}<br>
{len(book["songs"])} songs · lyrics for {found}{src}</div>{warn}</header>
<nav><ol>{"".join(toc)}</ol></nav>{"".join(body)}
<a class="top" href="#top">Songs ↑</a></main></body></html>"""


def save_book(book):
    OUT_DIR.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", _ascii(book["artist"]).lower()).strip("-") or "concert"
    path = OUT_DIR / f'{slug}-{book["iso_date"]}.html'
    path.write_text(render_book(book), encoding="utf-8")
    return path


def _ascii(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


# ---------------------------------------------------------------- web app

HOME = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Concert Lyrics</title>
<style>
:root{--bg:#fbf8f3;--fg:#1d1b19;--muted:#6f6860;--line:#d9d1c4;--accent:#8a3b2e;--err:#b03a2e}
@media (prefers-color-scheme:dark){:root{--bg:#121110;--fg:#ece6dd;--muted:#9a9187;--line:#3a3632;--accent:#e08a6f;--err:#ef7f6f}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 -apple-system,system-ui,sans-serif}
main{max-width:520px;margin:12vh auto 0;padding:0 16px}
h1{font:600 2rem/1.2 Georgia,serif;margin:0 0 6px}p{color:var(--muted);margin:0 0 24px}
form{display:flex;gap:8px}
input{flex:1;min-width:0;font:inherit;padding:12px 14px;border:1px solid var(--line);border-radius:10px;background:transparent;color:var(--fg)}
button{font:600 15px/1 -apple-system,system-ui,sans-serif;padding:0 18px;border:0;border-radius:10px;background:var(--accent);color:#fff;cursor:pointer}
button:disabled{opacity:.6}#msg{margin-top:16px;color:var(--muted)}#msg.err{color:var(--err)}
#sugg{display:flex;flex-direction:column;gap:8px;margin-top:12px}
#sugg button{background:transparent;color:var(--fg);border:1px solid var(--line);padding:12px 14px;text-align:left;font-weight:500}
#sugg small{color:var(--muted);font-weight:400}
.warn{border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:20px;font-size:14px}
.warn a{color:var(--accent)}
</style></head><body><main>
<h1>Concert Lyrics</h1>
<p>Enter who you're seeing. You'll get their latest setlist from setlist.fm with the lyrics to every song, on one page.</p>
__WARN__
<form id="f"><input id="a" placeholder="Artist name" autofocus required maxlength="100"><button id="b">Get lyrics</button></form>
<div id="msg"></div><div id="sugg"></div></main>
<script>
const $ = id => document.getElementById(id);
async function build(params) {
  const b = $('b'), m = $('msg'); $('sugg').innerHTML = '';
  b.disabled = true; m.className = ''; m.textContent = 'Finding the latest setlist and fetching lyrics… (about 10–30 seconds)';
  try {
    const r = await fetch('/build?' + new URLSearchParams(params));
    let j; try { j = await r.json(); } catch { throw new Error('The app stopped responding. Is it still running?'); }
    if (r.ok) { location.href = j.url; return; }
    m.className = 'err'; m.textContent = j.error;
    (j.suggestions || []).forEach(s => {
      const btn = document.createElement('button');
      btn.textContent = s.name;
      if (s.note) { const n = document.createElement('small'); n.textContent = '  ' + s.note; btn.append(n); }
      btn.onclick = () => { $('a').value = s.name; build({artist: s.name, mbid: s.mbid}); };
      $('sugg').append(btn);
    });
  } catch (e) { m.className = 'err'; m.textContent = e.message.includes('fetch') ? 'Can\\'t reach the app. Is it still running?' : e.message; }
  b.disabled = false;
}
$('f').onsubmit = ev => { ev.preventDefault(); build({artist: $('a').value.trim()}); };
</script></body></html>"""

KEY_WARNING = """<div class="warn">No setlist.fm API key set yet. Get a free one at
<a href="https://www.setlist.fm/settings/api" target="_blank">setlist.fm/settings/api</a>, then put
<code>SETLISTFM_API_KEY=your-key</code> in a <code>.env</code> file in this folder and restart.</div>"""


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, code, obj):
        self.send(code, json.dumps(obj), "application/json")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/":
            self.send(200, HOME.replace("__WARN__", "" if load_api_key() else KEY_WARNING))
        elif u.path == "/build":
            artist = (q.get("artist") or [""])[0]
            mbid = (q.get("mbid") or [""])[0] or None
            try:
                path = save_book(build_book(artist, load_api_key(), mbid))
                self.send_json(200, {"url": "/lyrics/" + urllib.parse.quote(path.name)})
            except NeedsChoice as e:
                self.send_json(409, {"error": str(e), "suggestions": e.suggestions})
            except AppError as e:
                self.send_json(400, {"error": str(e)})
            except Exception as e:
                self.send_json(500, {"error": f"Something unexpected went wrong ({type(e).__name__}). Try again."})
        elif u.path.startswith("/lyrics/"):
            f = OUT_DIR / Path(urllib.parse.unquote(u.path)).name
            if f.is_file():
                self.send(200, f.read_text(encoding="utf-8"))
            else:
                self.send(404, "Not found", "text/plain")
        else:
            self.send(404, "Not found", "text/plain")

    def log_message(self, *args):
        pass


# ---------------------------------------------------------------- command line

def run_cli(name):
    key, mbid = load_api_key(), None
    while True:
        try:
            book = build_book(name, key, mbid)
            break
        except NeedsChoice as e:
            print(e)
            for i, s in enumerate(e.suggestions, 1):
                print(f"  {i}. {s['name']}" + (f"  ({s['note']})" if s["note"] else ""))
            if not sys.stdin.isatty():
                sys.exit(2)
            pick = input("Pick a number (or press Enter to cancel): ").strip()
            if not pick.isdigit() or not 1 <= int(pick) <= len(e.suggestions):
                sys.exit("Cancelled.")
            chosen = e.suggestions[int(pick) - 1]
            name, mbid = chosen["name"], chosen["mbid"]
        except AppError as e:
            sys.exit(f"Error: {e}")
    path = save_book(book)
    found = [s for s in book["songs"] if s["lyrics"]]
    print(f'{book["artist"]}: {book["date"]} at {book["venue"]}')
    print(f'{len(book["songs"])} songs, lyrics found for {len(found)}')
    for s in book["songs"]:
        if not s["lyrics"]:
            print(f'  no lyrics: {s["title"]} ({s["status"]})')
    if book["old_warning"]:
        print("Note: " + book["old_warning"])
    print(f"Saved: {path}")


def main():
    if len(sys.argv) > 1:
        try:
            run_cli(" ".join(sys.argv[1:]))
        except KeyboardInterrupt:
            sys.exit("\nCancelled.")
        return
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        sys.exit(f"Port {PORT} is already in use. Is the app already running? "
                 f"Open http://localhost:{PORT} or run with PORT=8766 python3 app.py")
    print(f"Concert Lyrics running at http://localhost:{PORT}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
