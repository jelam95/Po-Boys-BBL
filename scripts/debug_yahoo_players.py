import json
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

def get_nba_game_key(session):
    url = f"{API_BASE}/games;game_keys=nba?format=json"
    r = session.get(url)
    r.raise_for_status()
    data = r.json()
    fc = data.get("fantasy_content", {})
    games = fc.get("games")
    # Try to grab the first game_key available
    if isinstance(games, dict):
        game_list = games.get("0", {}).get("game")
        if isinstance(game_list, dict) and "game_key" in game_list:
            return game_list["game_key"]
        elif isinstance(game_list, list):
            for g in game_list:
                if "game_key" in g:
                    return g["game_key"]
    return "nba"

if __name__ == "__main__":
    session = make_session_from_bearer()
    game_key = get_nba_game_key(session)
    print(f"Resolved NBA game key: {game_key}")
    url = f"{API_BASE}/game/{game_key}/players;start=0;count=5?format=json"
    print(f"Requesting: {url}")
    r = session.get(url)
    print("Status code:", r.status_code)
    print("Raw response JSON:")
    print(json.dumps(r.json(), indent=2, ensure_ascii=False)[:5000])  # print first 5000 chars