import os
import json
import unicodedata
import re

PLAYERS_JSON = "data/players.json"
OUTPUT_DIR = "Players"

def slugify(value):
    # Remove accents, replace spaces with underscores, keep only alphanumerics/underscores
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
    value = re.sub(r"[^\w\s-]", '', value).strip()
    value = re.sub(r"[\s]+", '_', value)
    return value

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(PLAYERS_JSON, "r", encoding="utf-8") as f:
        players = json.load(f)
    for player in players:
        name = player.get("name", "Unknown Player")
        filename = slugify(name) + ".md"
        path = os.path.join(OUTPUT_DIR, filename)
        player_id = player.get("player_id", "")
        team = player.get("nba_team", "")
        positions = player.get("eligible_positions", [])
        # Frontmatter
        frontmatter = [
            "---",
            f"player_id: {player_id}",
            f"name: {name}",
            f"team: {team}",
            f"positions: {positions}",
            "fantasy_teams: []",
            "---"
        ]
        # Body
        content = [
            f"# {name}",
            "",
            f"- **Yahoo Team:** {team}  ",
            f"- **Eligible Positions:** {', '.join(positions)}",
            "",
            "<!-- Add more player history below as needed -->"
        ]
        # Write file
        with open(path, "w", encoding="utf-8") as outf:
            outf.write("\n".join(frontmatter) + "\n\n")
            outf.write("\n".join(content) + "\n")
    print(f"Wrote {len(players)} player Markdown files to {OUTPUT_DIR}/")

if __name__ == "__main__":
    main()