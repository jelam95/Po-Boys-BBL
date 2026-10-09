# National Bastard Association: league archive

A read-only website for the National Bastard Association (Po Boys BBL) fantasy basketball league. Visitors land on a home page (league facts from the vault note `Silmaril/National Bastard Association.md`, links to each section, and the priciest current keepers), and can browse draft history, keeper history, the league rules and roster snapshots, and open any player to see their vault details and records. There are no accounts or editing, and the site makes no Yahoo calls.

- `site/`: the static site (`index.html`, `app.js`, `style.css`), which reads `site/archive.json`.
- `import_archive.py`: builds `site/archive.json` from the league workbooks, the vault's player notes and the vault's rules.
- `archive/manifest.json`: which workbook sheets to read and how their columns map to fields. Details are in [`archive/MAPPING.md`](archive/MAPPING.md).
- `archive/aliases.json`: explicit player-name aliases, each with a reason.
- `archive/REPORT.md`: the latest import and validation report.
- `tests/`: offline tests for the importer (synthetic workbooks) and for `fetch.py`.

The rest of the repository (`Silmaril/`, `scripts/`, `data/`, the loose notes) is older content and is not part of the site. The authoritative vault is the separate folder `../Po-Boys-BBL`.

## 1. Where source workbooks go

Put every workbook in `archive/workbooks/`. That folder is git-ignored, as is every `.xlsx` file, so workbooks are never committed or published. Leave the originals where they are and copy them in.

The current sources are `Keepers '26-'27 Season.xlsx` (2025-26 draft with pre-adjusted 2026-27 keeper prices, keepers and current rosters) and `Accuracy.xlsx` (the original 2025-26 draft prices).

The manifest lists each workbook and every sheet in it. A sheet is mapped as `draft`, `draft_prices` (extra price columns for draft picks, matched by pick and player), `keeper` or `roster`, or marked `ignore` with a reason. If a workbook has a sheet the manifest doesn't mention, the import stops, so nothing is skipped silently. To add a new season's draft or keepers, add the workbook to `archive/workbooks/` and an entry to `workbooks` in the manifest. Copy an existing entry and change `file`, `sheet`, `season` and the column headers.

## 2. Install the import dependency (once)

The importer needs `openpyxl` (pinned in `requirements-archive.txt`). The site itself has no dependencies.

```sh
cd ~/Documents/Claude/yahoo-league-site
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-archive.txt
```

## 3. Import or update the archive

```sh
.venv/bin/python import_archive.py           # import, validate, publish to site/archive.json
.venv/bin/python import_archive.py --check   # validate and write the report only
```

The importer reads the vault at `../Po-Boys-BBL` by default (use `--vault PATH` to point elsewhere). It then:

- Matches players by the Yahoo player link in the cell, then by an explicit alias, then by normalised name.
- Validates every row and writes `archive/REPORT.md`.
- Replaces `site/archive.json` only if there are no errors. A failed import changes nothing on the site.

Read the report's warnings after each run. The report also has a **Keeper price accuracy** section. It checks every recorded next-season keeper price against the draft price and the fee rules, and lists anything that doesn't add up. If nothing changed, the file and its import date are left as they were.

Rules come straight from the vault's `Silmaril/Rules.md`. To change a rule, edit the vault note and rerun the import. Never edit the rules in this repository. `Pre-Format Rules.md` is hidden from the site at the owner's request.

### Rule-change suggestions

The Rules page ends with a "Suggest a rule change" section. Its "Rules Amendments" link points there. Members fill in a Google Form that the commissioner owns, and the responses stay private in the commissioner's Google account. The site only links to the form and never sees submissions.

To open the form:

1. Create the form at forms.google.com.
2. Click Send → link, and copy the link.
3. Paste it into `suggestions.form_url` in `archive/manifest.json`. Only `https://docs.google.com/forms/...` or `https://forms.gle/...` links are accepted.
4. Rerun the import.

To close the form again, set `form_url` back to `null`. Edit `season` and `intro` there each year.

## 4. Adding active roster data

Rosters are imported as dated snapshots. Add an entry to `rosters` in `archive/manifest.json`:

```json
{"file": "Rosters 2026-11-01.xlsx", "sheet": "Rosters", "kind": "roster",
 "layout": "table", "header_row": 1, "columns": {"team": "Team", "player": "Player"},
 "season": "2026-27", "snapshot_date": "2026-11-01"}
```

- **Layouts.** Use `"layout": "table"` for one row per player with team and player columns. Use `"layout": "team_blocks"` for sheets like the current workbook's `Sheet3`, where each team name has a `Player` header under it and the players listed below.
- **Required fields.** `season` and `snapshot_date` (YYYY-MM-DD) are required. `label` (for example `"Pre-draft"`) is optional.
- **Keeper prices.** Add `"keeper_prices_from_draft": "2026-27"` to show each player's recorded keeper price from that season's draft. Add `"undrafted_price": {"amount": 15, "basis": "..."}` to give undrafted players a price.
- **Keep older snapshots.** Keep their workbooks and manifest entries: the importer refuses to drop a published snapshot.

The Rosters page shows the newest snapshot by default and lets visitors pick older ones. Until a snapshot is imported it says "Active roster data has not been imported."

## 5. Preview locally

```sh
python3 -m http.server 8000 -d site      # open http://localhost:8000
.venv/bin/python -m unittest discover -s tests -v
```

## 6. Publish and update the shared site

The site is hosted on GitHub Pages from this repository (`jelam95/Po-Boys-BBL`) through the *Publish league archive* workflow (`.github/workflows/update.yml`).

1. One time: under Settings → Pages, set **Source: GitHub Actions**. This replaces the old branch-built Jekyll site.
2. After each import, commit `site/archive.json`, `archive/REPORT.md` and any manifest or alias changes, then push.
3. Go to Actions → *Publish league archive* → *Run workflow*. The workflow runs the tests and checks that `archive.json` contains no private import metadata. It uploads exactly `index.html`, `app.js`, `style.css` and `archive.json`, and fails if `site/` holds anything else. It needs no secrets.

Share the Pages URL that the run prints. The page carries `noindex`, so search engines are asked not to list it, but anyone with the link can view it.

## Yahoo snapshot (not part of the archive)

`fetch.py` and `auth_once.py` are the earlier Yahoo standings tools. They still work locally: `python3 fetch.py --demo`, or `--live` after `auth_once.py`, writes the git-ignored `site/data.json`. The archive page does not read that file and the workflow does not deploy it. The two importers write different files, so neither overwrites the other. Credentials stay in the git-ignored `.secrets/`. Setup steps are in [`YAHOO.md`](YAHOO.md).
