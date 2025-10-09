import csv, json, time, os
from typing import List, Dict, Any
import requests

API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"

def read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def make_session_from_bearer() -> requests.Session:
    secrets = read_json("secrets.json")
    token = secrets.get("access_token")
    if not token:
        raise RuntimeError("No access_token in secrets.json.")
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return s

def get_nba_game_key(session) -> str:
    url = f"{API_BASE}/games;game_keys=nba?format=json"
    r = session.get(url)
    r.raise_for_status()
    data = r.json()
    fc = data.get("fantasy_content", {})
    games = fc.get("games")
    if isinstance(games, dict):
        game_list = games.get("0", {}).get("game")
        if isinstance(game_list, dict) and "game_key" in game_list:
            return game_list["game_key"]
        elif isinstance(game_list, list):
            for g in game_list:
                if "game_key" in g:
                    return g["game_key"]
    return "nba"

def fetch_players_page(session: requests.Session, game_key: str, start: int, count: int) -> List[Dict]:
    url = f"{API_BASE}/game/{game_key}/players;start={start};count={count}?format=json"
    r = session.get(url)
    if r.status_code != 200:
        print(f"HTTP {r.status_code} for {url}\n{r.text[:800]}")
        return []
    data = r.json()
    fc = data.get("fantasy_content", {})
    game = fc.get("game")
    # game is a list: [game_meta, {players: {...}}]
    if not isinstance(game, list) or len(game) < 2:
        print("[debug] Unexpected 'game' node:", json.dumps(game, ensure_ascii=False)[:400])
        return []
    players_node = game[1].get("players")
    if not players_node:
        print("[debug] No 'players' node on this page.")
        return []
    players: List[Dict] = []
    for k, v in players_node.items():
        if k == "count":
            continue
        player_outer = v.get("player")
        if not player_outer or not isinstance(player_outer, list) or not player_outer[0]:
            continue
        player_list = player_outer[0]  # The inner list of dicts
        flat = {"player_id": None, "name": None, "nba_team": None, "eligible_positions": []}
        for node in player_list:
            if not isinstance(node, dict):
                continue
            if "player_id" in node:
                flat["player_id"] = node["player_id"]
            if "name" in node and isinstance(node["name"], dict):
                flat["name"] = node["name"].get("full")
            if "editorial_team_abbr" in node:
                flat["nba_team"] = node["editorial_team_abbr"]
            if "eligible_positions" in node and isinstance(node["eligible_positions"], list):
                flat["eligible_positions"] = [
                    e.get("position") for e in node["eligible_positions"]
                    if isinstance(e, dict) and "position" in e
                ]
        if flat["player_id"] and flat["name"]:
            players.append(flat)
    return players

def fetch_all_players(session: requests.Session, game_key: str, per_page: int = 25, max_pages: int = 500) -> List[Dict]:
    all_players: List[Dict] = []
    start = 0
    for page in range(max_pages):
        print(f"[debug] Fetching page {page} (start={start}, count={per_page})")
        page_players = fetch_players_page(session, game_key, start=start, count=per_page)
        if page == 0:
            print(f"[debug] first page returned {len(page_players)} players (start={start}, count={per_page})")
        if not page_players:
            print(f"[debug] Stopping at page {page}, no players returned.")
            break
        all_players.extend(page_players)
        if len(page_players) < per_page:
            print(f"[debug] Last page reached (returned {len(page_players)} players, expected {per_page}).")
            break
        start += per_page
        time.sleep(0.4)
    return all_players

def write_outputs(players: List[Dict], json_path="data/players.json", csv_path="data/players.csv"):
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(players, f, ensure_ascii=False, indent=2)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["player_id", "name", "nba_team", "eligible_positions"])
        for p in players:
            w.writerow([
                p.get("player_id"),
                p.get("name"),
                p.get("nba_team"),
                ",".join(p.get("eligible_positions", []))
            ])

if __name__ == "__main__":
    session = make_session_from_bearer()
    print("Using bearer-token session from secrets.json")
    game_key = get_nba_game_key(session)
    print(f"Resolved NBA game key: {game_key}")
    players = fetch_all_players(session, game_key=game_key, per_page=25)
    print(f"Fetched {len(players)} players total")
    write_outputs(players)
    print("Wrote data/players.json and data/players.csv")