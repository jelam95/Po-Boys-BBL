# Source-to-field mapping

How each source becomes a field in `site/archive.json` (schema `po-boys-archive/1`). The explicit sheet and column mapping lives in `manifest.json`; this file explains it. Sources were inspected on 2026-10-09.

## Sources

| Source | What it contains | Used for |
| --- | --- | --- |
| Vault `Silmaril/players/*.md` (681 notes) | Frontmatter `player_id` (Yahoo player id), `name`, `team` (NBA), `positions`, `fantasy_teams` (empty in every note). Generated from the vault's `data/players.json` by `scripts/generate_player_markdown.py`; all 681 notes match that file exactly. | Player objects |
| Workbook `Keepers '26-'27 Season.xlsx`, sheet `Last Years Draft` | Excel table A1:D157. Columns Pick, Player, Salary, Team; 156 picks, 12 teams. Each Player cell links to `sports.yahoo.com/nba/players/<id>`. The owner pre-adjusted **Salary** to the price to keep each player for 2026-27; the cell colour shows which fee was applied (legend F2:F4). No formulas. | Draft records, 2025-26: pick, drafting team, 2026-27 keeper price, fee type |
| Workbook `Accuracy.xlsx`, sheet `Sheet1` | Pick, Player, Salary, Team (empty); the same 156 picks in the same order, with the same Yahoo links. Salary is the original 2025-26 draft price. No formulas. | Draft price for each 2025-26 draft record, and the accuracy check |
| `Keepers '26-'27 Season.xlsx`, sheet `Keepers` | Name, Price to Keep; 28 rows, no links, no team column. Four values are `CBK`. These are the players kept heading into 2025-26, with their updated price to keep them for 2026-27. No formulas. | Keeper records, 2025-26 |
| `Keepers '26-'27 Season.xlsx`, sheet `Sheet3` | Twelve team-name blocks (team name linked to the Yahoo team page), each with a `Player` header and the players listed below. These are the active rosters heading into the 2026-27 draft. | Roster snapshot "2026-27 Pre-draft", dated 2026-10-09 (owner confirmed) |
| Vault `Silmaril/Rules.md`, `Rules Amendments.md`, `Pre-Format Rules.md` | Current numbered rules; a one-line note linking to Pre-Format Rules; an older numbered rule set with struck-through text | Rules pages |
| Vault `Silmaril/Rules Ammendments.md` | Empty | Not published |

## Player

| Field | Source |
| --- | --- |
| `id` | Vault `player_id`, which is the Yahoo player id. A player with a Yahoo link but no vault note keeps the Yahoo id (`in_vault: false`). A player with neither gets a stable `u-<normalised name>` id. |
| `name`, `nba_team`, `positions` | Vault frontmatter `name`, `team`, `positions`, shown as "From the league vault". The vault notes are undated. |
| `in_vault` | `true` when a vault note exists |
| `aliases` | Every spelling seen in the sources that differs from the vault name, for example `Bobby Portis Jr.` or `Jalen Wiliams` |

Matching order: (1) Yahoo player id from the cell's hyperlink; (2) an explicit alias in `aliases.json`; (3) a normalised name that matches exactly one vault player. Normalisation strips accents, lower-cases the name and removes everything except letters and digits, so `Nikola Jokic` matches `Nikola Jokić`. It does not drop suffixes such as `Jr.`. When a normalised name matches more than one vault player, it is reported and kept unmatched. Players are never merged because their names merely look alike.

## Draft record (`drafts`)

| Field | Source (`Last Years Draft`) |
| --- | --- |
| `season` | Manifest: `2025-26` (owner confirmed) |
| `pick` | `Pick` (whole number ≥ 1) |
| `source_name`, `nba_team`, `positions` | `Player`, split from `Name (TEAM - POS,POS)`. The TEAM and POS text is as written in the workbook, which may reflect the export date rather than draft day. Where it differs from the vault, `REPORT.md` lists the difference. |
| `player_id` | `Player` hyperlink, then the matching order above |
| `team` | `Team`, kept exactly as written (for example `Vlade’s Dance` with a curly apostrophe) |
| `salary`, `salary_display`, `salary_note` | The draft price, from `Accuracy.xlsx` `Salary`. A row is attached by season and pick, and must link to the same Yahoo player; otherwise it is rejected. A conflicting price is also rejected. Numbers are whole dollars, with `$0` kept as 0; a blank cell becomes null and is shown as "—". |
| `next_price`, `next_price_display`, `next_price_note` | The draft sheet's `Salary`: the owner's pre-adjusted 2026-27 keeper price. `CBK` gives `next_price: null`, `next_price_display: "CBK"` and `next_price_note: "Can't Be Kept"`. |
| `fee_key`, `fee_type`, `fee_short` | Fill colour of the draft sheet's Salary cell, looked up in the legend cells (F2 yellow "Consecutive Year Keeper Fee", F3 cyan "First time keeper fee", F4 green "Drafted for Sub $10 fee"; red has no legend cell and the owner confirmed it means CBK). The owner said the colour shows which fee applies for 2026-27 (`fee_types["2025-26"].applies_to`). `fee_type.display` in the manifest maps each legend label to the wording and colour key shown on the site (owner approved 2026-10-09): `first` "1st-year keeper: +$5 or 10%", `repeat` "Repeat keeper: +$7 or 10%", `under10` "Under $10: raised to $10, +$5", `cbk` "Can't be kept". `fee_short` is the compact roster-card version. If a legend label has no display entry, the import stops. The site colours follow the spreadsheet (cyan, yellow, green, red). No price is calculated. |
| `kept` | `true` when the Keepers sheet has the same player in the same season |

## Keeper record (`keepers`)

| Field | Source (`Keepers`) |
| --- | --- |
| `season` | Manifest: `2025-26`. The owner described these as "pre defined values from players that were kept the season before last". The names and prices match draft picks 129-156 of the 2025-26 draft, so they are recorded as 2025-26 keepers. Confirm the season label. |
| `source_name`, `player_id` | `Name` (no links); matched by normalised name, or by alias for `Jalen Wiliams` |
| `team`, `draft_pick` | From the single 2025-26 draft record for the same player (`team_from_draft`). If there is no draft record, or more than one, the row is rejected. |
| `cost`, `cost_display`, `cost_note` | The price the player was kept at for 2025-26, which is that draft record's draft price from `Accuracy.xlsx` (`from_draft: ["cost"]`). |
| `next_cost`, `next_cost_display`, `next_cost_note` | `Price to Keep`: the updated price to keep the player for 2026-27. If it differs from the draft record's `next_price`, a warning is reported. |

## Roster snapshot (`rosters`)

`{id, season, label, snapshot_date, price_from_draft, undrafted_price, undrafted_rule, entries: [...]}`. The season, label and date come from the manifest entry. Rosters are never inferred from drafts or keepers.

Each entry is `{team, player_id, source_name, keeper_price, keeper_price_display, keeper_price_note, price_basis, drafted_by, draft_pick}`:

- **Drafted in `price_from_draft` (2025-26):** the price is that draft record's recorded 2026-27 keeper price (`price_basis: "drafted"`). `drafted_by` shows the drafting team, which can differ from the current roster team.
- **Not drafted:** the price is the manifest's `undrafted_price` ($15) with its stated basis (`price_basis: "undrafted"`, `fee_key: "undrafted"`, shown as a green dashed "Undrafted" badge). This is the owner's rule from 2026-10-09: Rule 1.c rounds undrafted players up to $10, plus the $5 first-time fee. The importer does not derive it.

## Accuracy check (report only)

`accuracy_check` in the manifest lists, for each fee colour key, the price band and fee rule from Rules 1.b and 1.c:

- **First time keeper fee:** draft price $10 or more; plus the larger of 10% or $5.
- **Drafted for Sub $10 fee:** draft price under $10, raised to $10; plus $5.
- **Consecutive Year Keeper Fee:** plus the larger of 10% or $7.

The importer checks each recorded 2026-27 price against its draft price and writes the result to `REPORT.md`. Fees are whole dollars: 10% is rounded to the nearest dollar, with 50 cents rounding up (owner, 2026-10-09; for example, 10% of $55 is $5.50, so the fee is $6). Rows where rounding applied are listed. CBK rows are listed but not checked. The check never changes or publishes a price.

## Teams

`teams` lists the fantasy team names that appear in the records, exactly as written. The sources contain no renames, so no team mapping exists. Names that differ only in punctuation or capitals are reported, not merged.

## Rules

Each vault note becomes `{id, title, status, source, blocks}`. Numbered and bulleted lists nest by indentation (a tab counts as 4 spaces), and the source numbers are kept. Bold, italic, strikethrough and `[[links]]` are preserved, and the text is never reworded. Links to another published rules note become in-site links. Other links, such as `[[index]]`, are shown with a visible "(unavailable: …)" note. `Pre-Format Rules` is labelled historical, because the current `Rules.md` links to it through `Rules Amendments` and it contains wording that has since been struck through or replaced.

## Not published

- Source file paths, sheet row numbers and import traceability. These go to `archive/build/records.json`, which is git-ignored.
- Vault `fantasy_teams`, which is empty everywhere, and team-note manager names.
- Workbooks.
