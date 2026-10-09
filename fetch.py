#!/usr/bin/env python3
"""Generate site/data.json for the Po Boys BBL league site.

Modes:
  --demo   Fictional data. Makes no network requests.
  --live   Refresh the Yahoo OAuth token and read standings and the current
           scoreboard. Credentials come from environment variables (GitHub
           Actions) or, with --local-secrets, from the file auth_once.py wrote.

Standard library only. Never prints tokens or raw API responses.
"""
import argparse
import base64
import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2/"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
DEFAULT_SECRETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".secrets", "yahoo.json")
DEFAULT_OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site", "data.json")
SCHEMA_VERSION = 1
STALE_AFTER_HOURS = 12

# Yahoo league scoring_type values.
SCORING_LABELS = {
    "head": "Head-to-head categories",
    "headone": "Head-to-head one win",
    "headpoint": "Head-to-head points",
    "roto": "Rotisserie",
    "point": "Season points",
}
HEAD_TO_HEAD = {"head", "headone", "headpoint"}

HTTP_HINTS = {
    400: "Check the request parameters, redirect URI and authorization grant.",
    401: "Authorization failed or the token is invalid/expired.",
    403: "Forbidden. Inspect Yahoo's error message: app permissions, Fantasy read access, or league membership.",
    404: "Not found. Check LEAGUE_ID and that it belongs to the current NBA season.",
    429: "Rate limited. Wait before retrying.",
    999: "Yahoo blocked the request. Wait and investigate before retrying.",
}


class FetchError(RuntimeError):
    """A failure that should stop the run with a readable message."""


class ParseError(ValueError):
    """Yahoo returned a shape this script does not understand."""


# --------------------------------------------------------------------------
# HTTP

def _error_summary(error):
    """Short, token-free description of a Yahoo error body."""
    try:
        body = json.loads(error.read().decode("utf-8", "replace"))
    except Exception:
        return ""
    if isinstance(body, dict):
        for key in ("error_description", "error"):
            if isinstance(body.get(key), str):
                return body[key][:200]
        inner = body.get("error")
        if isinstance(inner, dict) and isinstance(inner.get("description"), str):
            return inner["description"][:200]
    return ""


def request_json(url, headers, body=None, opener=urllib.request.urlopen):
    request = urllib.request.Request(url, data=body, headers=headers)
    try:
        with opener(request, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        detail = _error_summary(error)
        hint = HTTP_HINTS.get(error.code, "Check Yahoo service availability.")
        raise FetchError("HTTP %s from %s. %s%s" % (
            error.code, urllib.parse.urlsplit(url).path, hint,
            (" Yahoo said: " + detail) if detail else "")) from None
    except urllib.error.URLError as error:
        raise FetchError("Network error contacting Yahoo: %s" % error.reason) from None
    try:
        return json.loads(raw)
    except ValueError:
        raise FetchError("Yahoo returned a response that is not JSON (%s)." % urllib.parse.urlsplit(url).path) from None


# --------------------------------------------------------------------------
# OAuth

def refresh_access_token(creds, opener=urllib.request.urlopen):
    """Exchange the refresh token. Returns (access_token, new_refresh_token or None)."""
    basic = base64.b64encode(("%s:%s" % (creds["client_id"], creds["client_secret"])).encode()).decode()
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": creds["refresh_token"],
        "redirect_uri": creds["redirect_uri"],
    }).encode()
    token = request_json(TOKEN_URL, {
        "Authorization": "Basic " + basic,
        "Content-Type": "application/x-www-form-urlencoded",
    }, body, opener)
    access = token.get("access_token") if isinstance(token, dict) else None
    if not access:
        raise FetchError("Token response did not include an access token.")
    rotated = token.get("refresh_token")
    if rotated and rotated != creds["refresh_token"]:
        return access, rotated
    return access, None


def write_private(path, text):
    """Atomically write text to path, readable by the owner only."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    temporary = path + ".tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(temporary, path)


def persist_rotated_token(rotated, creds, secrets_file=None, rotated_file=None):
    """Save a rotated refresh token before anything else can fail."""
    if secrets_file:
        stored = dict(creds)
        stored["refresh_token"] = rotated
        stored["refreshed_at"] = utc_now()
        write_private(secrets_file, json.dumps(stored, indent=2) + "\n")
    if rotated_file:
        write_private(rotated_file, rotated)


# --------------------------------------------------------------------------
# Yahoo JSON helpers. Yahoo's JSON wraps objects as lists of single-key
# dicts and collections as {"0": ..., "1": ..., "count": n}.

def merge(value):
    """Flatten Yahoo's list-of-fragments object form into one dict."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        result = {}
        for item in value:
            if isinstance(item, (list, dict)):
                result.update(merge(item))
            else:
                raise ParseError("Unexpected scalar inside a Yahoo object")
        return result
    raise ParseError("Expected a Yahoo object, got %s" % type(value).__name__)


def collection(value, what):
    """Return entries of a numbered Yahoo collection, in order."""
    if isinstance(value, list) and not value:
        return []
    if not isinstance(value, dict):
        raise ParseError("Expected a numbered collection of %s" % what)
    numbered = [key for key in value if key.isdigit()]
    unknown = [key for key in value if key != "count" and not key.isdigit()]
    if unknown:
        raise ParseError("Unexpected key %r in %s collection" % (unknown[0], what))
    if "count" in value and int(value["count"]) != len(numbered):
        raise ParseError("%s collection count does not match its entries" % what)
    return [value[key] for key in sorted(numbered, key=int)]


def require(mapping, key, what):
    if not isinstance(mapping, dict) or key not in mapping:
        raise ParseError("Missing %r in %s" % (key, what))
    return mapping[key]


def _number(value):
    if value in (None, "", "-"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ParseError("Expected a number, got %r" % (value,))
    return int(number) if number.is_integer() else number


# --------------------------------------------------------------------------
# Parsers

def parse_league_meta(league):
    meta = merge(league)
    scoring = require(meta, "scoring_type", "league metadata")
    if scoring not in SCORING_LABELS:
        raise ParseError("Unsupported scoring_type %r" % scoring)
    return {
        "name": str(require(meta, "name", "league metadata")),
        "season": str(meta.get("season", "")),
        "scoring_type": scoring,
        "scoring_label": SCORING_LABELS[scoring],
        "current_week": _number(meta.get("current_week")),
        "is_finished": bool(_number(meta.get("is_finished")) or 0),
    }


def parse_standings(league, scoring):
    league = merge(league)
    standings = merge(require(league, "standings", "league"))
    rows = []
    for wrapper in collection(require(standings, "teams", "standings"), "teams"):
        team = merge(require(wrapper, "team", "team entry"))
        stats = merge(require(team, "team_standings", "team"))
        row = {
            "rank": _number(stats.get("rank")),
            "name": str(require(team, "name", "team")),
        }
        if scoring in HEAD_TO_HEAD:
            totals = require(stats, "outcome_totals", "team standings")
            row["wins"] = _number(require(totals, "wins", "outcome totals"))
            row["losses"] = _number(require(totals, "losses", "outcome totals"))
            row["ties"] = _number(totals.get("ties")) or 0
            row["pct"] = str(totals.get("percentage", ""))
            row["games_back"] = str(stats.get("games_back", ""))
            if scoring == "headpoint":
                row["points_for"] = _number(stats.get("points_for"))
                row["points_against"] = _number(stats.get("points_against"))
        else:
            row["points"] = _number(require(stats, "points_for", "team standings"))
        rows.append(row)
    if not rows:
        raise ParseError("Standings contained no teams")
    # Unranked teams (preseason) keep Yahoo's order after ranked ones.
    rows.sort(key=lambda r: (r["rank"] is None, r["rank"] if r["rank"] is not None else 0))
    return rows


def parse_scoreboard(league):
    """Return (week, [{"teams": [a, b], "status": s}]). Empty list only for a
    scoreboard Yahoo explicitly reports as having no matchups."""
    league = merge(league)
    scoreboard = require(league, "scoreboard", "league")
    if not isinstance(scoreboard, dict):
        raise ParseError("Unexpected scoreboard shape")
    week = _number(scoreboard.get("week"))
    body = require(scoreboard, "0", "scoreboard")
    matchups = require(merge(body), "matchups", "scoreboard")
    pairs = []
    for wrapper in collection(matchups, "matchups"):
        matchup = merge(require(wrapper, "matchup", "matchup entry"))
        week = week if week is not None else _number(matchup.get("week"))
        teams = require(merge(require(matchup, "0", "matchup")), "teams", "matchup")
        names = [str(require(merge(require(t, "team", "matchup team")), "name", "team"))
                 for t in collection(teams, "matchup teams")]
        if len(names) != 2:
            raise ParseError("A matchup had %d teams, expected 2" % len(names))
        pairs.append({"teams": names, "status": str(matchup.get("status", ""))})
    return week, pairs


# --------------------------------------------------------------------------
# Data assembly

def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def demo_data():
    teams = [
        ("Bayou Bricklayers", 48, 30, 4),
        ("Gumbo Goons", 46, 33, 3),
        ("Fictional & Fast", 44, 35, 3),
        ('The "Quoted" <Team>', 41, 37, 4),
        ("Café Crescent Ballers", 40, 40, 2),
        ("Levee Breakers", 37, 42, 3),
        ("Swamp Hawks", 33, 46, 3),
        ("Example Team Eight", 30, 49, 3),
    ]
    leader = teams[0][1] - teams[0][2]
    standings = []
    for rank, (name, w, l, t) in enumerate(teams, 1):
        games = w + l + t
        standings.append({
            "rank": rank, "name": name, "wins": w, "losses": l, "ties": t,
            "pct": "%.3f" % ((w + t / 2) / games) if games else "",
            "games_back": "-" if rank == 1 else "%g" % ((leader - (w - l)) / 2),
        })
    order = [0, 7, 1, 6, 2, 5, 3, 4]
    pairs = [{"teams": [teams[order[i]][0], teams[order[i + 1]][0]], "status": "midevent"}
             for i in range(0, len(order), 2)]
    return {
        "mode": "demo",
        "league": {"name": "Example Basketball League (fictional)", "season": "2026",
                   "scoring_type": "head", "scoring_label": SCORING_LABELS["head"],
                   "current_week": 3, "is_finished": False},
        "standings": standings,
        "matchups": {"week": 3, "applicable": True, "pairs": pairs},
    }


def live_data(creds, league_id, game_key=None, secrets_file=None, rotated_file=None,
              opener=urllib.request.urlopen):
    access, rotated = refresh_access_token(creds, opener)
    if rotated:
        persist_rotated_token(rotated, creds, secrets_file, rotated_file)
    headers = {"Authorization": "Bearer " + access, "Accept": "application/json"}

    def get(path):
        content = request_json(API_BASE + path + "?format=json", headers, opener=opener)
        return require(content, "fantasy_content", "response")

    if not game_key:
        game = merge(require(get("game/nba"), "game", "game response"))
        game_key = str(require(game, "game_key", "game"))
    league_key = "%s.l.%s" % (game_key, league_id)

    standings_league = require(get("league/%s/standings" % league_key), "league", "standings response")
    meta = parse_league_meta(standings_league[0] if isinstance(standings_league, list) else standings_league)
    standings = parse_standings(standings_league, meta["scoring_type"])

    if meta["scoring_type"] in HEAD_TO_HEAD:
        week, pairs = parse_scoreboard(
            require(get("league/%s/scoreboard" % league_key), "league", "scoreboard response"))
        matchups = {"week": week, "applicable": True, "pairs": pairs}
    else:
        matchups = {"week": None, "applicable": False, "pairs": []}
    return {"mode": "live", "league": meta, "standings": standings, "matchups": matchups}, bool(rotated)


def load_env_creds():
    names = {"client_id": "YAHOO_CLIENT_ID", "client_secret": "YAHOO_CLIENT_SECRET",
             "refresh_token": "YAHOO_REFRESH_TOKEN", "redirect_uri": "YAHOO_REDIRECT_URI"}
    missing = [env for env in names.values() if not os.environ.get(env)]
    if missing:
        raise FetchError("Missing environment variables: " + ", ".join(missing))
    return {key: os.environ[env] for key, env in names.items()}


def load_file_creds(path):
    try:
        with open(path, encoding="utf-8") as handle:
            creds = json.load(handle)
    except FileNotFoundError:
        raise FetchError("No credentials at %s. Run auth_once.py first." % path) from None
    missing = [k for k in ("client_id", "client_secret", "refresh_token", "redirect_uri") if not creds.get(k)]
    if missing:
        raise FetchError("Credentials file is missing: " + ", ".join(missing))
    return creds


def write_output(data, path):
    data = dict(data)
    data["schema"] = SCHEMA_VERSION
    data["updated"] = utc_now()
    data["stale_after_hours"] = STALE_AFTER_HOURS
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
        handle.write("\n")
    os.replace(temporary, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true", help="write fictional data; no network")
    mode.add_argument("--live", action="store_true", help="read the league from Yahoo")
    parser.add_argument("--local-secrets", nargs="?", const=DEFAULT_SECRETS_FILE, metavar="PATH",
                        help="read credentials from auth_once.py's file (default .secrets/yahoo.json)")
    parser.add_argument("--league-id", default=os.environ.get("LEAGUE_ID"))
    parser.add_argument("--game-key", default=os.environ.get("YAHOO_GAME_KEY"),
                        help="override the current-season NBA game key")
    parser.add_argument("--out", default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    try:
        if args.demo:
            data = demo_data()
        else:
            if not args.league_id or not args.league_id.isdigit():
                raise FetchError("LEAGUE_ID must be the numeric ID from the league URL.")
            if args.local_secrets:
                creds, secrets_file = load_file_creds(args.local_secrets), args.local_secrets
            else:
                creds, secrets_file = load_env_creds(), None
            data, rotated = live_data(creds, args.league_id, args.game_key, secrets_file,
                                      os.environ.get("ROTATED_TOKEN_FILE"))
            if rotated:
                print("Yahoo issued a new refresh token; it was saved privately.")
        write_output(data, args.out)
    except (FetchError, ParseError) as error:
        kind = "Unexpected Yahoo response" if isinstance(error, ParseError) else "Fetch failed"
        print("%s: %s" % (kind, error), file=sys.stderr)
        return 1
    print("Wrote %s data: %d teams, %d matchups (%s)." % (
        data["mode"], len(data["standings"]), len(data["matchups"]["pairs"]), data["league"]["scoring_label"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
