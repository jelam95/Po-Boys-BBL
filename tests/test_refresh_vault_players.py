"""Offline tests for refresh_vault_players.py note planning (no Yahoo calls)."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import refresh_vault_players as rv  # noqa: E402


def player(pid, name, team, positions):
    return {"player_id": pid, "name": name, "nba_team": team, "eligible_positions": positions}


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def note(self, filename, text):
        with open(os.path.join(self.dir, filename), "w", encoding="utf-8") as f:
            f.write(text)

    def test_note_format_matches_vault_generator(self):
        text = rv.render_note(player("5294", "Joel Embiid", "PHI", ["C"]))
        self.assertEqual(text, "---\nplayer_id: 5294\nname: Joel Embiid\nteam: PHI\npositions: ['C']\n"
                               "fantasy_teams: []\n---\n\n# Joel Embiid\n\n- **Yahoo Team:** PHI  \n"
                               "- **Eligible Positions:** C\n\n<!-- Add more player history below as needed -->\n")

    def test_updates_generated_notes_only_and_never_deletes(self):
        self.note("Trae_Young.md", rv.render_note(player("1", "Trae Young", "ATL", ["PG"])))
        self.note("Joel_Embiid.md", rv.render_note(player("2", "Joel Embiid", "PHI", ["C"])))
        edited = rv.render_note(player("3", "Hand Edited", "AAA", ["C"])) + "My notes\n"
        self.note("Hand_Edited.md", edited)
        self.note("Gone_Player.md", rv.render_note(player("4", "Gone Player", "BBB", ["SF"])))
        self.note("Sam_Smith.md", rv.render_note(player("5", "Sam Smith", "CCC", ["PG"])))
        result = rv.plan([player("1", "Trae Young", "WAS", ["PG"]),        # moved team
                          player("2", "Joel Embiid", "PHI", ["C"]),        # unchanged
                          player("3", "Hand Edited", "DDD", ["C"]),        # edited note: leave alone
                          player("5", "Sam Smith", "CCC", ["PG"]),
                          player("9", "Sam Smith", "EEE", ["SG"])],        # new player, filename clash
                         self.dir)
        self.assertEqual([f for f, _ in result["update"]], ["Trae_Young.md"])
        self.assertIn("team: WAS", result["update"][0][1])
        self.assertEqual(result["unchanged"], 2)
        self.assertEqual(result["edited"], ["Hand_Edited.md"])
        self.assertEqual([f for f, _, _ in result["new"]], ["Sam_Smith_9.md"])
        self.assertEqual(result["missing"], ["Gone Player"])
        self.assertEqual(result["changes"], [("Trae Young", "ATL PG", "WAS PG", "")])


if __name__ == "__main__":
    unittest.main()
