"""Offline tests for app.py. Every network call is faked, so no API key or internet is needed."""

import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402

SINGER = "11111111-1111-1111-1111-111111111111"
NAMESAKE = "22222222-2222-2222-2222-222222222222"
OTHER = "33333333-3333-3333-3333-333333333333"


def setlist(artist="Test Artist", day="21-11-2026", sets=None):
    return {
        "artist": {"name": artist},
        "eventDate": day,
        "venue": {"name": "Big Arena", "city": {"name": "Newark", "country": {"name": "United States"}}},
        "tour": {"name": "The Tour"},
        "url": "https://www.setlist.fm/setlist/example.html",
        "sets": {
            "set": sets
            if sets is not None
            else [
                {"song": [{"name": "Intro", "tape": True}, {"name": "First Song"}, {"name": "Second Song"}]},
                {"encore": 1, "song": [{"name": "Cover Song", "cover": {"name": "Original Band"}}]},
            ]
        },
    }


class FakeWeb:
    """Stands in for app.get_json and answers based on the URL."""

    def __init__(self, artists=(), setlists=None, musicbrainz=(), lyrics=None):
        self.artists = list(artists)  # setlist.fm artist search results
        self.setlists = setlists or {}  # mbid -> list of setlists
        self.musicbrainz = list(musicbrainz)  # MusicBrainz search results
        self.lyrics = lyrics or {}  # (title, artist or None) -> LRCLIB results
        self.urls = []

    def __call__(self, url, headers=None, tries=4):
        self.urls.append(url)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        if "search/artists" in url:
            return {"artist": self.artists}
        if "/setlists" in url:
            mbid = url.split("/artist/")[1].split("/")[0]
            return {"setlist": self.setlists.get(mbid, [])}
        if "musicbrainz.org" in url:
            return {"artists": self.musicbrainz}
        if "lrclib.net" in url:
            return self.lyrics.get((query.get("track_name"), query.get("artist_name")), [])
        raise AssertionError(f"unexpected URL {url}")


class TextHelpers(unittest.TestCase):
    def test_norm_ignores_case_accents_punctuation_and_leading_the(self):
        self.assertEqual(app.norm("The Killers"), app.norm("killers"))
        self.assertEqual(app.norm("Beyoncé"), "beyonce")
        self.assertEqual(app.norm("Simon & Garfunkel"), app.norm("simon and garfunkel"))
        self.assertEqual(app.norm(None), "")

    def test_similarity_scores_typos_and_partial_names_as_close(self):
        self.assertGreaterEqual(app.similarity("chapel roan", "Chappell Roan"), 0.6)
        self.assertGreaterEqual(app.similarity("olivia", "Olivia Rodrigo"), 0.7)
        self.assertLess(app.similarity("chapel roan", "Joan Baez"), 0.6)
        self.assertEqual(app.similarity("", "Anyone"), 0.0)

    def test_load_api_key_reads_env_file(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, ".env").write_text('# comment\nSETLISTFM_API_KEY="abc123"\n')
            with mock.patch.object(app, "HERE", Path(d)), mock.patch.dict("os.environ", {}, clear=True):
                self.assertEqual(app.load_api_key(), "abc123")

    def test_environment_variable_wins_over_env_file(self):
        with mock.patch.dict("os.environ", {"SETLISTFM_API_KEY": "from-env"}):
            self.assertEqual(app.load_api_key(), "from-env")


class GetJson(unittest.TestCase):
    def response(self, data):
        return mock.MagicMock(__enter__=lambda s: io.BytesIO(json.dumps(data).encode()))

    def http_error(self, code):
        return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(b""))

    @mock.patch.object(app.time, "sleep")
    def test_retries_when_server_is_busy(self, _sleep):
        with mock.patch.object(urllib.request, "urlopen", side_effect=[self.http_error(503), self.response({"ok": 1})]):
            self.assertEqual(app.get_json("https://x"), {"ok": 1})

    @mock.patch.object(app.time, "sleep")
    def test_gives_up_after_max_tries(self, _sleep):
        with mock.patch.object(urllib.request, "urlopen", side_effect=self.http_error(503)) as m:
            with self.assertRaises(urllib.error.HTTPError):
                app.get_json("https://x", tries=3)
            self.assertEqual(m.call_count, 3)

    def test_404_returns_none_without_retrying(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=self.http_error(404)) as m:
            self.assertIsNone(app.get_json("https://x"))
            self.assertEqual(m.call_count, 1)

    def test_auth_errors_are_not_retried(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=self.http_error(403)) as m:
            with self.assertRaises(urllib.error.HTTPError):
                app.get_json("https://x")
            self.assertEqual(m.call_count, 1)


class SetlistFm(unittest.TestCase):
    def test_missing_key_explains_how_to_get_one(self):
        with self.assertRaisesRegex(app.AppError, "No setlist.fm API key"):
            app.setlistfm("search/artists", "", artistName="x")

    def test_rejected_key_gets_friendly_message(self):
        error = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b""))
        with mock.patch.object(app, "get_json", side_effect=error):
            with self.assertRaisesRegex(app.AppError, "rejected the API key"):
                app.setlistfm("search/artists", "key", artistName="x")

    def test_no_internet_gets_friendly_message(self):
        with mock.patch.object(app, "get_json", side_effect=urllib.error.URLError("down")):
            with self.assertRaisesRegex(app.AppError, "Couldn't reach setlist.fm"):
                app.setlistfm("search/artists", "key", artistName="x")


class FindArtist(unittest.TestCase):
    def find(self, name, web):
        with mock.patch.object(app, "get_json", web):
            return app.find_artist(name, "key")

    def test_exact_match_any_capitalization(self):
        web = FakeWeb(artists=[{"name": "The Killers", "mbid": SINGER}, {"name": "Killers Tribute", "mbid": OTHER}])
        self.assertEqual(self.find("killers", web)["mbid"], SINGER)

    def test_same_name_picks_the_artist_who_plays_shows(self):
        web = FakeWeb(
            artists=[{"name": "Olivia Rodrigo", "mbid": SINGER}, {"name": "Olivia Rodrigo", "mbid": NAMESAKE}],
            setlists={SINGER: [setlist()], NAMESAKE: []},
        )
        self.assertEqual(self.find("Olivia Rodrigo", web)["mbid"], SINGER)

    def test_same_name_both_active_asks_which(self):
        web = FakeWeb(
            artists=[
                {"name": "OLIVIA", "mbid": SINGER},
                {"name": "OLIVIA", "mbid": NAMESAKE},
                {"name": "Olivia Dean", "mbid": OTHER},
            ],
            setlists={SINGER: [setlist()], NAMESAKE: [setlist()]},
        )
        with self.assertRaises(app.NeedsChoice) as ctx:
            self.find("Olivia", web)
        self.assertEqual([s["mbid"] for s in ctx.exception.suggestions], [SINGER, NAMESAKE, OTHER])

    def test_typo_offers_suggestions_instead_of_guessing(self):
        web = FakeWeb(
            artists=[], musicbrainz=[{"name": "Chappell Roan", "id": SINGER}, {"name": "Joan Baez", "id": OTHER}]
        )
        with self.assertRaises(app.NeedsChoice) as ctx:
            self.find("chapel roan", web)
        names = [s["name"] for s in ctx.exception.suggestions]
        self.assertEqual(names[0], "Chappell Roan")
        self.assertNotIn("Joan Baez", names)

    def test_nothing_close_says_check_spelling(self):
        with self.assertRaisesRegex(app.AppError, "Check the spelling"):
            self.find("xqzv blarg", FakeWeb())

    def test_musicbrainz_failure_does_not_break_lookup(self):
        def web(url, *args, **kwargs):
            if "musicbrainz" in url:
                raise urllib.error.URLError("busy")
            return {"artist": []}

        with mock.patch.object(app, "get_json", web):
            with self.assertRaisesRegex(app.AppError, "Check the spelling"):
                app.find_artist("xqzv blarg", "key")


class Setlists(unittest.TestCase):
    def test_songs_skip_tape_and_keep_sections_and_covers(self):
        songs = app.songs_in(setlist())
        self.assertEqual([s["title"] for s in songs], ["First Song", "Second Song", "Cover Song"])
        self.assertEqual(songs[2]["section"], "Encore 1")
        self.assertEqual(songs[2]["artist"], "Original Band")
        self.assertTrue(songs[2]["is_cover"])

    def test_latest_setlist_skips_shows_without_songs(self):
        empty = setlist(day="30-11-2026", sets=[])
        web = FakeWeb(setlists={SINGER: [empty, setlist()]})
        with mock.patch.object(app, "get_json", web):
            self.assertEqual(app.latest_setlist(SINGER, "key")["eventDate"], "21-11-2026")

    def test_latest_setlist_none_when_no_show_has_songs(self):
        web = FakeWeb(setlists={SINGER: [setlist(sets=[])]})
        with mock.patch.object(app, "get_json", web):
            self.assertIsNone(app.latest_setlist(SINGER, "key"))


class Lyrics(unittest.TestCase):
    def fetch(self, title, artist, lyrics):
        with mock.patch.object(app, "get_json", FakeWeb(lyrics=lyrics)):
            return app.fetch_lyrics(title, artist)

    def test_finds_lyrics_for_the_right_artist(self):
        found = self.fetch(
            "Song",
            "Singer",
            {("Song", "Singer"): [{"artistName": "Singer", "trackName": "Song", "plainLyrics": "la la\n"}]},
        )
        self.assertEqual(found, ("la la", None))

    def test_never_uses_another_artists_song_with_the_same_title(self):
        lyrics, missing = self.fetch(
            "Song",
            "Singer",
            {("Song", None): [{"artistName": "Someone Else", "trackName": "Song", "plainLyrics": "wrong"}]},
        )
        self.assertIsNone(lyrics)
        self.assertIn("not in the lyrics database", missing)

    def test_falls_back_to_title_only_search(self):
        found = self.fetch(
            "Song",
            "Singer",
            {("Song", None): [{"artistName": "Singer feat. Friend", "trackName": "Song", "plainLyrics": "ok"}]},
        )
        self.assertEqual(found, ("ok", None))

    def test_skips_other_artists_to_reach_a_featured_version(self):
        found = self.fetch(
            "Song",
            "Singer",
            {
                ("Song", None): [
                    {"artistName": "Someone Else", "trackName": "Song", "plainLyrics": "wrong"},
                    {"artistName": "Singer feat. Friend", "trackName": "Song", "plainLyrics": "right"},
                ]
            },
        )
        self.assertEqual(found, ("right", None))

    def test_instrumental(self):
        self.assertEqual(
            self.fetch(
                "Song",
                "Singer",
                {("Song", "Singer"): [{"artistName": "Singer", "trackName": "Song", "instrumental": True}]},
            ),
            (None, "instrumental"),
        )

    def test_lyrics_site_down(self):
        with mock.patch.object(app, "get_json", side_effect=urllib.error.URLError("down")):
            self.assertEqual(app.fetch_lyrics("Song", "Singer"), (None, "the lyrics site didn't respond"))


class BuildAndRender(unittest.TestCase):
    def web(self):
        return FakeWeb(
            artists=[{"name": "Test Artist", "mbid": SINGER}],
            setlists={SINGER: [setlist()]},
            lyrics={
                ("First Song", "Test Artist"): [
                    {"artistName": "Test Artist", "trackName": "First Song", "plainLyrics": "<b>words</b>"}
                ],
                ("Cover Song", "Original Band"): [
                    {"artistName": "Original Band", "trackName": "Cover Song", "plainLyrics": "cover words"}
                ],
            },
        )

    def build(self, name="Test Artist", mbid=None):
        with mock.patch.object(app, "get_json", self.web()):
            return app.build_book(name, "key", mbid)

    def test_input_validation(self):
        for name, message in [("", "Enter an artist name"), ("   ", "Enter an artist name"), ("x" * 101, "too long")]:
            with self.subTest(name=name), self.assertRaisesRegex(app.AppError, message):
                self.build(name)
        with self.assertRaisesRegex(app.AppError, "Invalid artist id"):
            self.build("x", mbid="../../etc/passwd")

    def test_build_book(self):
        book = self.build()
        self.assertEqual(book["date"], date(2026, 11, 21))
        self.assertEqual(book["place"], "Newark, United States")
        self.assertEqual([s["lyrics"] is not None for s in book["songs"]], [True, False, True])

    def test_rendered_page(self):
        page = app.render_book(self.build())
        self.assertIn("November 21, 2026", page)
        self.assertIn("3 songs · lyrics for 2", page)
        self.assertIn('<div class="sec">Encore 1</div>', page)
        self.assertIn('<ol start="3">', page)
        self.assertIn("(no lyrics)", page)
        self.assertIn("Cover of Original Band", page)
        self.assertIn("&lt;b&gt;words&lt;/b&gt;", page)  # lyrics are escaped, never raw HTML
        self.assertNotIn("$", page.split("<body>")[1])  # every template slot was filled

    def test_old_setlist_warning(self):
        book = self.build()
        self.assertEqual(app.old_warning(book), "")
        book["date"] = date.today() - timedelta(days=200)
        self.assertIn("200 days old", app.old_warning(book))

    def test_save_book_names_file_after_artist_and_date(self):
        book = self.build()
        book["artist"] = "Beyoncé & Friends"
        with tempfile.TemporaryDirectory() as d, mock.patch.object(app, "OUT_DIR", Path(d)):
            path = app.save_book(book)
            self.assertEqual(path.name, "beyonce-friends-2026-11-21.html")
            self.assertTrue(path.read_text().startswith("<!doctype html>"))


class WebApp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = mock.patch.object(app, "OUT_DIR", Path(cls.tmp.name))
        cls.out.start()
        Path(cls.tmp.name, "saved.html").write_text("<p>saved page</p>")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.out.stop()
        cls.tmp.cleanup()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_home_page(self):
        with mock.patch.object(app, "load_api_key", return_value="key"):
            status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn('id="artist"', body)
        self.assertNotIn("No setlist.fm API key", body)

    def test_home_page_warns_without_key(self):
        with mock.patch.object(app, "load_api_key", return_value=""):
            self.assertIn("No setlist.fm API key", self.get("/")[1])

    def test_build_returns_suggestions_for_typos(self):
        choice = app.NeedsChoice("Did you mean?", [{"name": "Chappell Roan", "mbid": SINGER, "note": ""}])
        with mock.patch.object(app, "build_book", side_effect=choice):
            status, body = self.get("/build?artist=chapel+roan")
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["suggestions"][0]["name"], "Chappell Roan")

    def test_build_reports_user_errors(self):
        status, body = self.get("/build?artist=")
        self.assertEqual((status, json.loads(body)["error"]), (400, "Enter an artist name."))

    def test_build_hides_unexpected_crashes(self):
        with mock.patch.object(app, "build_book", side_effect=KeyError("x")):
            status, body = self.get("/build?artist=x")
        self.assertEqual(status, 500)
        self.assertIn("Something unexpected went wrong", json.loads(body)["error"])

    def test_serves_saved_pages_but_blocks_path_tricks(self):
        self.assertEqual(self.get("/lyrics/saved.html"), (200, "<p>saved page</p>"))
        self.assertEqual(self.get("/lyrics/..%2F.env")[0], 404)
        self.assertEqual(self.get("/nope")[0], 404)


if __name__ == "__main__":
    unittest.main()
