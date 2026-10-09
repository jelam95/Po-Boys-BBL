"""Offline tests. Fixtures are synthetic, modelled on Yahoo's JSON shapes; they
do not prove live compatibility."""
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auth_once  # noqa: E402
import fetch  # noqa: E402

CREDS = {"client_id": "cid", "client_secret": "csecret",
         "refresh_token": "old-refresh", "redirect_uri": "https://localhost:8080/callback"}


# ---------------------------------------------------------------- fixtures

def team(team_id, name, standings, extra=None):
    meta = [{"team_key": "466.l.1.t.%d" % team_id}, {"team_id": str(team_id)}, {"name": name},
            [], {"url": "https://example.invalid"}, {"managers": [{"manager": {"nickname": "x"}}]}]
    parts = [meta, {"team_points": {"coverage_type": "season", "total": "0"}},
             {"team_standings": standings}]
    if extra:
        parts.append(extra)
    return {"team": parts}


def league_meta(scoring="head", **extra):
    meta = {"league_key": "466.l.1", "name": "Synthetic League", "season": "2026",
            "scoring_type": scoring, "current_week": "3", "is_finished": 0}
    meta.update(extra)
    return meta


def numbered(entries):
    result = {str(i): entry for i, entry in enumerate(entries)}
    result["count"] = len(entries)
    return result


def standings_league(scoring, teams):
    return [league_meta(scoring), {"standings": [{"teams": numbered(teams)}]}]


def h2h_standing(rank, w, l, t, **extra):
    s = {"rank": rank, "playoff_seed": str(rank), "games_back": "-" if rank == 1 else "2.5",
         "outcome_totals": {"wins": str(w), "losses": str(l), "ties": str(t), "percentage": ".600"}}
    s.update(extra)
    return s


def scoreboard_league(pairs, week="3"):
    matchups = numbered([{"matchup": {"0": {"teams": numbered([team(i, a, {}), team(i + 1, b, {})])},
                                      "week": week, "status": "midevent", "is_tied": 0}}
                         for i, (a, b) in enumerate(pairs)])
    return [league_meta(), {"scoreboard": {"0": {"matchups": matchups}, "week": week}}]


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Routes requests by URL substring; records calls."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.calls.append(request)
        for needle, result in self.routes:
            if needle in url:
                if isinstance(result, Exception):
                    raise result
                if callable(result):
                    result = result(request)
                if isinstance(result, bytes):
                    return FakeResponse(result)
                return FakeResponse(json.dumps(result).encode())
        raise AssertionError("unexpected URL " + url)


def http_error(code, body=b"{}"):
    return urllib.error.HTTPError("https://x.invalid", code, "err", {}, io.BytesIO(body))


def wrap(content):
    return {"fantasy_content": content}


def live_routes(token, standings, scoreboard):
    return [
        ("get_token", token),
        ("game/nba", wrap({"game": [{"game_key": "466", "code": "nba", "season": "2026"}]})),
        ("/standings", wrap({"league": standings})),
        ("/scoreboard", wrap({"league": scoreboard})),
    ]


# ---------------------------------------------------------------- parsers

class ParserTests(unittest.TestCase):
    def test_h2h_categories_standings(self):
        league = standings_league("head", [
            team(2, "Second <b>& Co</b>", h2h_standing(2, 40, 42, 0)),
            team(1, "First", h2h_standing(1, 50, 30, 2)),
        ])
        rows = fetch.parse_standings(league, "head")
        self.assertEqual([r["name"] for r in rows], ["First", "Second <b>& Co</b>"])
        self.assertEqual((rows[0]["wins"], rows[0]["losses"], rows[0]["ties"]), (50, 30, 2))
        self.assertEqual(rows[1]["games_back"], "2.5")

    def test_headpoint_includes_points(self):
        league = standings_league("headpoint", [
            team(1, "A", h2h_standing(1, 5, 1, 0, points_for="812.5", points_against="700")),
        ])
        row = fetch.parse_standings(league, "headpoint")[0]
        self.assertEqual((row["points_for"], row["points_against"]), (812.5, 700))

    def test_roto_uses_points(self):
        league = standings_league("roto", [team(1, "A", {"rank": "1", "points_for": "88.5"})])
        self.assertEqual(fetch.parse_standings(league, "roto")[0]["points"], 88.5)

    def test_h2h_missing_outcomes_is_error(self):
        league = standings_league("head", [team(1, "A", {"rank": "1", "points_for": "5"})])
        with self.assertRaises(fetch.ParseError):
            fetch.parse_standings(league, "head")

    def test_preseason_unranked_keeps_order(self):
        league = standings_league("head", [
            team(1, "A", h2h_standing("", 0, 0, 0)), team(2, "B", h2h_standing("", 0, 0, 0))])
        self.assertEqual([r["name"] for r in fetch.parse_standings(league, "head")], ["A", "B"])

    def test_count_mismatch_is_error(self):
        teams = numbered([team(1, "A", h2h_standing(1, 1, 0, 0))])
        teams["count"] = 2
        with self.assertRaises(fetch.ParseError):
            fetch.parse_standings([league_meta(), {"standings": [{"teams": teams}]}], "head")

    def test_league_meta(self):
        meta = fetch.parse_league_meta(league_meta("headpoint"))
        self.assertEqual((meta["scoring_type"], meta["current_week"]), ("headpoint", 3))

    def test_unknown_scoring_type_is_error(self):
        with self.assertRaises(fetch.ParseError):
            fetch.parse_league_meta(league_meta("mystery"))

    def test_scoreboard_pairs(self):
        week, pairs = fetch.parse_scoreboard(scoreboard_league([("A", "B"), ("C", "D")]))
        self.assertEqual(week, 3)
        self.assertEqual([p["teams"] for p in pairs], [["A", "B"], ["C", "D"]])

    def test_explicitly_empty_scoreboard(self):
        for empty in ({"count": 0}, []):
            league = [league_meta(), {"scoreboard": {"0": {"matchups": empty}, "week": "1"}}]
            self.assertEqual(fetch.parse_scoreboard(league), (1, []))

    def test_malformed_scoreboards_are_errors(self):
        bad = [
            [league_meta(), {"scoreboard": []}],
            [league_meta(), {"scoreboard": {"week": "1"}}],
            [league_meta(), {"scoreboard": {"0": {}, "week": "1"}}],
            [league_meta()],
        ]
        for league in bad:
            with self.subTest(league=league), self.assertRaises(fetch.ParseError):
                fetch.parse_scoreboard(league)

    def test_matchup_with_one_team_is_error(self):
        league = [league_meta(), {"scoreboard": {"0": {"matchups": numbered([
            {"matchup": {"0": {"teams": numbered([team(1, "A", {})])}}}])}, "week": "1"}}]
        with self.assertRaises(fetch.ParseError):
            fetch.parse_scoreboard(league)


# ---------------------------------------------------------------- live flow

class LiveFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def standings(self):
        return standings_league("head", [team(1, "A", h2h_standing(1, 3, 1, 0)),
                                         team(2, "B", h2h_standing(2, 1, 3, 0))])

    def test_no_rotation(self):
        opener = FakeOpener(live_routes({"access_token": "acc", "refresh_token": "old-refresh"},
                                        self.standings(), scoreboard_league([("A", "B")])))
        data, rotated = fetch.live_data(CREDS, "1", rotated_file=self.path("rot"), opener=opener)
        self.assertFalse(rotated)
        self.assertFalse(os.path.exists(self.path("rot")))
        self.assertEqual(data["matchups"]["pairs"][0]["teams"], ["A", "B"])
        token_request = opener.calls[0]
        self.assertTrue(token_request.get_header("Authorization").startswith("Basic "))
        body = urllib.parse.parse_qs(token_request.data.decode())
        self.assertEqual(body["redirect_uri"], [CREDS["redirect_uri"]])

    def test_rotation_saved_even_if_parse_fails(self):
        opener = FakeOpener(live_routes({"access_token": "acc", "refresh_token": "new-refresh"},
                                        [league_meta(), {"standings": "garbage"}], {}))
        secrets_file = self.path("yahoo.json")
        with self.assertRaises(fetch.ParseError):
            fetch.live_data(CREDS, "1", secrets_file=secrets_file, rotated_file=self.path("rot"),
                            opener=opener)
        with open(self.path("rot")) as handle:
            self.assertEqual(handle.read(), "new-refresh")
        with open(secrets_file) as handle:
            self.assertEqual(json.load(handle)["refresh_token"], "new-refresh")
        for name in ("rot", "yahoo.json"):
            self.assertEqual(stat.S_IMODE(os.stat(self.path(name)).st_mode), 0o600)

    def test_rotation_saved_even_if_http_fails(self):
        routes = live_routes({"access_token": "acc", "refresh_token": "new-refresh"}, None, None)
        routes[1] = ("game/nba", http_error(503))
        with self.assertRaises(fetch.FetchError):
            fetch.live_data(CREDS, "1", rotated_file=self.path("rot"), opener=FakeOpener(routes))
        self.assertTrue(os.path.exists(self.path("rot")))

    def test_roto_skips_scoreboard(self):
        roto = standings_league("roto", [team(1, "A", {"rank": "1", "points_for": "10"})])
        roto[0]["scoring_type"] = "roto"
        opener = FakeOpener(live_routes({"access_token": "acc"}, roto, AssertionError("no")))
        data, _ = fetch.live_data(CREDS, "1", opener=opener)
        self.assertFalse(data["matchups"]["applicable"])

    def test_http_errors_are_readable_and_token_free(self):
        opener = FakeOpener([("get_token", http_error(401, b'{"error":"invalid_grant"}'))])
        with self.assertRaises(fetch.FetchError) as caught:
            fetch.live_data(CREDS, "1", opener=opener)
        message = str(caught.exception)
        self.assertIn("401", message)
        self.assertIn("invalid_grant", message)
        for secret in CREDS.values():
            if secret != CREDS["redirect_uri"]:
                self.assertNotIn(secret, message)

    def test_non_json_response(self):
        opener = FakeOpener([("get_token", b"<html>blocked</html>")])
        with self.assertRaises(fetch.FetchError):
            fetch.live_data(CREDS, "1", opener=opener)

    def test_missing_access_token(self):
        with self.assertRaises(fetch.FetchError):
            fetch.live_data(CREDS, "1", opener=FakeOpener([("get_token", {"error": "x"})]))


# ---------------------------------------------------------------- CLI / output

class OutputTests(unittest.TestCase):
    def test_demo_writes_valid_data_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "data.json")
            original = fetch.urllib.request.urlopen
            fetch.urllib.request.urlopen = None  # any network use would fail
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(fetch.main(["--demo", "--out", out]), 0)
            finally:
                fetch.urllib.request.urlopen = original
            with open(out, encoding="utf-8") as handle:
                data = json.load(handle)
        self.assertEqual((data["mode"], data["schema"]), ("demo", 1))
        self.assertTrue(data["standings"] and data["matchups"]["pairs"])
        self.assertIn("fictional", data["league"]["name"])

    def test_live_requires_numeric_league_id(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(fetch.main(["--live", "--league-id", "abc", "--out", os.devnull]), 1)
        self.assertIn("LEAGUE_ID", err.getvalue())


# ---------------------------------------------------------------- authorization

class AuthTests(unittest.TestCase):
    REDIRECT = "https://localhost:8080/callback"

    def test_auth_url(self):
        url = auth_once.build_auth_url("cid", self.REDIRECT, "st8")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(query["redirect_uri"], [self.REDIRECT])
        self.assertEqual(query["state"], ["st8"])
        self.assertEqual(query["response_type"], ["code"])

    def test_extract_code_valid(self):
        url = self.REDIRECT + "?code=abc123&state=st8"
        self.assertEqual(auth_once.extract_code("  " + url + "\n", "st8", self.REDIRECT), "abc123")

    def test_extract_code_rejects(self):
        cases = [
            self.REDIRECT + "?code=abc&state=wrong",
            self.REDIRECT + "?code=abc",
            self.REDIRECT + "?state=st8",
            self.REDIRECT + "?code=abc&state=st8&state=st8",
            "https://evil.example/callback?code=abc&state=st8",
            self.REDIRECT + "?error=access_denied&state=st8",
            "abc123",
        ]
        for url in cases:
            with self.subTest(url=url), self.assertRaises(auth_once.AuthError):
                auth_once.extract_code(url, "st8", self.REDIRECT)

    def test_exchange_code(self):
        opener = FakeOpener([("get_token", {"access_token": "a", "refresh_token": "r"})])
        self.assertEqual(auth_once.exchange_code("cid", "sec", self.REDIRECT, "code", opener), "r")
        body = urllib.parse.parse_qs(opener.calls[0].data.decode())
        self.assertEqual(body["grant_type"], ["authorization_code"])
        self.assertEqual(body["redirect_uri"], [self.REDIRECT])

    def test_exchange_without_refresh_token(self):
        with self.assertRaises(auth_once.AuthError):
            auth_once.exchange_code("c", "s", self.REDIRECT, "code",
                                    FakeOpener([("get_token", {"access_token": "a"})]))


if __name__ == "__main__":
    unittest.main()
