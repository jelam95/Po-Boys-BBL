#!/usr/bin/env python3
"""Refresh the vault's NBA player list (team, positions) from Yahoo.

Uses the credentials auth_once.py saved in .secrets/yahoo.json and the token
code in fetch.py (a rotated refresh token is saved back first; tokens are
never printed). Writes, in the vault's existing formats:

  <vault>/data/players.json, data/players.csv   the full Yahoo player list
  <vault>/Silmaril/players/*.md                 one note per player

A note is rewritten only if it still matches what the vault's
scripts/generate_player_markdown.py would have produced for its current
frontmatter, so hand-edited notes are left alone and reported. New players
get new notes. Nothing is deleted: players Yahoo no longer lists keep their
notes. Standard library only.

Usage:
  python3 refresh_vault_players.py --dry-run   fetch and report, write nothing to the vault
  python3 refresh_vault_players.py             fetch and update the vault
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import time
import unicodedata

import fetch
import import_archive

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_VAULT = os.path.normpath(os.path.join(ROOT, "..", "Po-Boys-BBL"))
PAGE = 25


def slugify(value):
    """Same as the vault's generate_player_markdown.py."""
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s-]", "", value).strip()
    return re.sub(r"[\s]+", "_", value)


def render_note(player):
    """Same layout as the vault's generate_player_markdown.py."""
    positions = player["eligible_positions"]
    front = ["---", "player_id: %s" % player["player_id"], "name: %s" % player["name"],
             "team: %s" % player["nba_team"], "positions: %s" % positions, "fantasy_teams: []", "---"]
    body = ["# %s" % player["name"], "", "- **Yahoo Team:** %s  " % player["nba_team"],
            "- **Eligible Positions:** %s" % ", ".join(positions), "",
            "<!-- Add more player history below as needed -->"]
    return "\n".join(front) + "\n\n" + "\n".join(body) + "\n"


def parse_players_page(content):
    game = fetch.require(content, "game", "players response")
    if not isinstance(game, list) or len(game) < 2:
        return []
    node = game[1].get("players") if isinstance(game[1], dict) else None
    if not node:
        return []
    players = []
    for key, value in node.items():
        if key == "count":
            continue
        outer = value.get("player") if isinstance(value, dict) else None
        if not outer or not isinstance(outer, list) or not outer[0]:
            continue
        flat = {"player_id": None, "name": None, "nba_team": None, "eligible_positions": []}
        for item in outer[0]:
            if not isinstance(item, dict):
                continue
            if "player_id" in item:
                flat["player_id"] = str(item["player_id"])
            if isinstance(item.get("name"), dict):
                flat["name"] = item["name"].get("full")
            if "editorial_team_abbr" in item:
                flat["nba_team"] = item["editorial_team_abbr"]
            if isinstance(item.get("eligible_positions"), list):
                flat["eligible_positions"] = [e["position"] for e in item["eligible_positions"]
                                              if isinstance(e, dict) and "position" in e]
        if flat["player_id"] and flat["name"]:
            players.append(flat)
    return players


def fetch_players(secrets_file):
    creds = fetch.load_file_creds(secrets_file)
    access, rotated = fetch.refresh_access_token(creds)
    if rotated:
        fetch.persist_rotated_token(rotated, creds, secrets_file)
    headers = {"Authorization": "Bearer " + access, "Accept": "application/json"}

    def get(path):
        content = fetch.request_json(fetch.API_BASE + path + "?format=json", headers)
        return fetch.require(content, "fantasy_content", "response")

    game = fetch.merge(fetch.require(get("game/nba"), "game", "game response"))
    game_key = str(fetch.require(game, "game_key", "game"))
    season = game.get("season")
    players, start = [], 0
    while True:
        page = parse_players_page(get("game/%s/players;start=%d;count=%d" % (game_key, start, PAGE)))
        players.extend(page)
        if len(page) < PAGE:
            break
        start += PAGE
        if start > 5000:
            raise fetch.FetchError("Stopped after 5000 players; the player list did not end.")
        time.sleep(0.4)
    return season, players


def plan(players, notes_dir):
    """Work out note changes without writing anything."""
    existing = {}        # player_id -> (filename, text, frontmatter)
    for filename in sorted(os.listdir(notes_dir)):
        if not filename.endswith(".md"):
            continue
        with open(os.path.join(notes_dir, filename), encoding="utf-8") as handle:
            text = handle.read()
        fm = import_archive.parse_frontmatter(text)
        if fm and fm.get("player_id"):
            existing[fm["player_id"]] = (filename, text, fm)
    taken = {name.lower() for name in os.listdir(notes_dir)}
    result = {"update": [], "unchanged": 0, "new": [], "edited": [], "changes": [], "missing": []}
    seen = set()
    for p in players:
        seen.add(p["player_id"])
        new_text = render_note(p)
        if p["player_id"] in existing:
            filename, text, fm = existing[p["player_id"]]
            old = {"player_id": fm["player_id"], "name": fm.get("name", ""), "nba_team": fm.get("team", ""),
                   "eligible_positions": import_archive.parse_list_field(fm.get("positions"))}
            if text == new_text:
                result["unchanged"] += 1
            elif text != render_note(old):
                result["edited"].append(filename)          # hand-edited: leave alone
            else:
                result["update"].append((filename, new_text))
                result["changes"].append((p["name"], "%s %s" % (old["nba_team"], ",".join(old["eligible_positions"])),
                                          "%s %s" % (p["nba_team"], ",".join(p["eligible_positions"])),
                                          old["name"] if old["name"] != p["name"] else ""))
        else:
            filename = slugify(p["name"]) + ".md"
            if filename.lower() in taken:
                filename = "%s_%s.md" % (slugify(p["name"]), p["player_id"])
            taken.add(filename.lower())
            result["new"].append((filename, new_text, p["name"]))
    result["missing"] = sorted(existing[pid][2].get("name", pid) for pid in existing if pid not in seen)
    return result


def players_csv(players):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["player_id", "name", "nba_team", "eligible_positions"])
    for p in players:
        writer.writerow([p["player_id"], p["name"], p["nba_team"], ",".join(p["eligible_positions"])])
    return out.getvalue()


def write_text(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    os.replace(tmp, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="report only; write nothing to the vault")
    parser.add_argument("--vault", default=DEFAULT_VAULT)
    parser.add_argument("--secrets", default=fetch.DEFAULT_SECRETS_FILE)
    parser.add_argument("--report", help="also write the summary to this file")
    args = parser.parse_args(argv)

    notes_dir = os.path.join(args.vault, "Silmaril", "players")
    if not os.path.isdir(notes_dir):
        print("Vault player notes not found: %s" % notes_dir, file=sys.stderr)
        return 1
    try:
        season, players = fetch_players(args.secrets)
    except fetch.FetchError as exc:
        print("Yahoo refresh failed: %s" % exc, file=sys.stderr)
        print("Nothing was written to the vault.", file=sys.stderr)
        return 1
    ids = [p["player_id"] for p in players]
    if not players or len(ids) != len(set(ids)):
        print("Yahoo returned %d players (%d unique ids); refusing to write." % (len(players), len(set(ids))),
              file=sys.stderr)
        return 1
    result = plan(players, notes_dir)

    lines = ["Yahoo NBA season %s: %d players fetched." % (season, len(players)),
             "Notes: %d updated, %d already current, %d new, %d hand-edited (left alone), %d not in Yahoo's list (kept)." % (
                 len(result["update"]), result["unchanged"], len(result["new"]), len(result["edited"]),
                 len(result["missing"]))]
    if result["changes"]:
        lines += ["", "Changed (old -> new):"] + ["- %s: %s -> %s%s" % (n, o, w, (" (was named %s)" % r) if r else "")
                                                 for n, o, w, r in result["changes"]]
    if result["new"]:
        lines += ["", "New notes:"] + ["- %s (%s)" % (name, fn) for fn, _, name in result["new"]]
    if result["edited"]:
        lines += ["", "Hand-edited notes left alone:"] + ["- " + fn for fn in result["edited"]]
    if result["missing"]:
        lines += ["", "In the vault but not in Yahoo's list (kept):"] + ["- " + n for n in result["missing"]]
    summary = "\n".join(lines) + "\n"
    if args.report:
        write_text(args.report, summary)
    print(summary if len(summary) < 4000 else "\n".join(lines[:2]) + "\n(full list in --report file)")

    if args.dry_run:
        print("Dry run: nothing written to the vault.")
        return 0
    data_dir = os.path.join(args.vault, "data")
    os.makedirs(data_dir, exist_ok=True)
    write_text(os.path.join(data_dir, "players.json"), json.dumps(players, ensure_ascii=False, indent=2))
    write_text(os.path.join(data_dir, "players.csv"), players_csv(players))
    for filename, text in result["update"]:
        write_text(os.path.join(notes_dir, filename), text)
    for filename, text, _ in result["new"]:
        write_text(os.path.join(notes_dir, filename), text)
    print("Vault updated. Rerun import_archive.py to publish.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
