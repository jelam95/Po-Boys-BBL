#!/usr/bin/env python3
"""Build site/archive.json for the Po Boys BBL league archive.

Reads the workbooks and sheet mappings listed in archive/manifest.json, the
player notes and rules in the Obsidian vault, and archive/aliases.json. Writes:

  site/archive.json              published archive data (players, drafts,
                                 keepers, roster snapshots, rules)
  archive/REPORT.md              import and validation report
  archive/build/records.json     local traceability (file, sheet, row per
                                 record); git-ignored, never published

Nothing is written if validation fails, so the last good site/archive.json
stays in place. Reruns are deterministic: if the data has not changed, the
existing file (and its import date) is kept.

Requires openpyxl (see requirements-archive.txt). The importer reads the values
Excel saved for formula cells; it never calculates formulas.

Usage:
  python3 import_archive.py              import and publish
  python3 import_archive.py --check      validate and report only
"""
import argparse
import ast
import datetime
import json
import os
import re
import sys
import tempfile
import unicodedata

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MANIFEST = os.path.join(ROOT, "archive", "manifest.json")
DEFAULT_ALIASES = os.path.join(ROOT, "archive", "aliases.json")
DEFAULT_OUTPUT = os.path.join(ROOT, "site", "archive.json")
DEFAULT_REPORT = os.path.join(ROOT, "archive", "REPORT.md")
DEFAULT_RECORDS = os.path.join(ROOT, "archive", "build", "records.json")
SCHEMA = "po-boys-archive/1"

YAHOO_PLAYER_LINK = re.compile(r"/nba/players/(\d+)")
NAME_TEAM_POS = re.compile(r"^(?P<name>.+?)\s*\((?P<team>[A-Za-z]{2,4})\s*-\s*(?P<pos>[A-Za-z, ]+)\)\s*$")
MONEY = re.compile(r"^\$?\s*(\d+(?:\.\d+)?)$")
SEASON = re.compile(r"^'?(\d{2}|\d{4})\s*[-–/]\s*'?(\d{2}|\d{4})$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# Money columns a sheet may map. Drafts: the price paid at the draft ("salary")
# and the recorded price to keep the player the following season ("next_price").
# Keepers: the price the player was kept at ("cost") and the recorded price to
# keep him again the following season ("next_cost").
MONEY_FIELDS = {"draft": ("salary", "next_price"), "keeper": ("cost", "next_cost")}


class ImportError_(Exception):
    """A structural problem that stops the import."""


# --------------------------------------------------------------------------
# Normalisation

def normalize_name(name):
    """Lower-case ASCII letters and digits only: 'Nikola Jokić' -> 'nikolajokic'."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", text.lower())


def clean_text(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_season(label):
    """'2025-26', "'25-'26", '2025–2026' -> '2025-26'. Raises ValueError."""
    match = SEASON.match(clean_text(label))
    if not match:
        raise ValueError("season %r is not like 2025-26" % label)
    start, end = match.groups()
    start = int(start) + (2000 if len(start) == 2 else 0)
    end = int(end[-2:])
    if (start + 1) % 100 != end:
        raise ValueError("season %r does not span consecutive years" % label)
    return "%d-%02d" % (start, end)


def season_key(label):
    return int(label[:4])


def parse_money(value, text_values):
    """Return (amount, display, note, problem). Zero stays 0; blank is None."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, None, None, None
    if isinstance(value, bool):
        return None, None, None, "invalid amount %r" % value
    if isinstance(value, (int, float)):
        amount = value
    else:
        text = clean_text(value)
        if text in text_values:
            return None, text, text_values[text], None
        match = MONEY.match(text)
        if not match:
            return None, None, None, "invalid amount %r" % text
        amount = float(match.group(1))
    if amount < 0:
        return None, None, None, "negative amount %r" % value
    amount = int(amount) if float(amount).is_integer() else round(float(amount), 2)
    return amount, "$%s" % amount, None, None


def parse_pick(value):
    """Return (pick, problem)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, None
    if isinstance(value, bool):
        return None, "invalid pick %r" % value
    try:
        number = float(clean_text(value)) if isinstance(value, str) else float(value)
    except ValueError:
        return None, "invalid pick %r" % value
    if not number.is_integer() or number < 1:
        return None, "invalid pick %r" % value
    return int(number), None


# --------------------------------------------------------------------------
# Report

class Report:
    def __init__(self):
        self.sheets = []        # dicts with counts and reasons
        self.errors = []        # block publishing
        self.warnings = []      # published, but need the owner's attention
        self.notes = []
        self.disagreements = []  # (player, workbook value, vault value, where)
        self.accuracy = None     # result of check_accuracy

    def error(self, message):
        self.errors.append(message)

    def warn(self, message):
        self.warnings.append(message)


class SheetStats:
    def __init__(self, workbook, sheet, kind):
        self.workbook, self.sheet, self.kind = workbook, sheet, kind
        self.rows_seen = 0
        self.imported = 0
        self.skipped = {}     # reason -> [rows]
        self.rejected = []    # (row, reason)
        self.note = ""

    def skip(self, row, reason):
        self.skipped.setdefault(reason, []).append(row)

    def reject(self, row, reason, report):
        self.rejected.append((row, reason))
        report.error("%s / %s row %d: %s" % (self.workbook, self.sheet, row, reason))


# --------------------------------------------------------------------------
# Vault

def parse_frontmatter(text):
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end == -1:
        return None
    fields = {}
    for line in text[4:end].splitlines():
        if ":" not in line or line.startswith((" ", "\t", "-")):
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def parse_list_field(value):
    value = (value or "").strip()
    if not value:
        return []
    try:
        parsed = ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return [part.strip() for part in value.strip("[]").split(",") if part.strip()]
    return [str(item) for item in parsed] if isinstance(parsed, (list, tuple)) else [str(parsed)]


def short_person(name):
    """'Pat Example' -> 'Pat E.'; several people separated by commas are each shortened.
    Applied at import so full names never reach the published file."""
    out = []
    for person in [p.strip() for p in name.split(",") if p.strip()]:
        parts = person.split()
        out.append(parts[0] if len(parts) == 1 else "%s %s." % (parts[0], parts[-1][0].upper()))
    return ", ".join(out)


def team_slug(name):
    """Stable team id from its name: "Vlade’s Dance" -> "vlades-dance"."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"['’`]", "", text)
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-") or "team"


def load_vault_teams(vault, cfg, report):
    """Team notes: Markdown files in the configured vault folder whose frontmatter has
    team_id and name. Only the fields listed in the manifest are published."""
    folder = os.path.join(vault, cfg.get("vault_dir", "Silmaril"))
    teams = {}
    if not os.path.isdir(folder):
        return teams
    for filename in sorted(os.listdir(folder)):
        if not filename.endswith(".md"):
            continue
        with open(os.path.join(folder, filename), encoding="utf-8") as handle:
            fm = parse_frontmatter(handle.read())
        if not fm or not fm.get("team_id") or not fm.get("name"):
            continue
        lowered = {k.lower(): v for k, v in fm.items()}
        info = {}
        for field in cfg.get("fields", []):
            value = lowered.get(field.lower(), "").strip().strip('"').strip("'")
            value = WIKI_LINK.sub(lambda m: (m.group(2) or m.group(1)).strip(), value)
            if value and field in cfg.get("_people_fields", ()):
                value = short_person(value)
            if value:
                info[field] = value
        teams[normalize_name(fm["name"])] = {"note": filename, "name": fm["name"].strip(), "info": info}
    return teams


def load_vault_players(players_dir, report):
    if not os.path.isdir(players_dir):
        raise ImportError_("vault players folder not found: %s" % os.path.basename(players_dir))
    players = {}
    for filename in sorted(os.listdir(players_dir)):
        if not filename.endswith(".md"):
            continue
        with open(os.path.join(players_dir, filename), encoding="utf-8") as handle:
            text = handle.read()
        fields = parse_frontmatter(text)
        if fields is None:
            if text.strip():
                report.warn("Vault note %s has no frontmatter; not used as a player." % filename)
            continue
        player_id = fields.get("player_id", "").strip()
        name = fields.get("name", "").strip()
        if not player_id or not name:
            report.warn("Vault note %s lacks player_id or name; not used." % filename)
            continue
        if player_id in players:
            report.error("Vault player_id %s appears in %s and %s." % (
                player_id, players[player_id]["_note"], filename))
            continue
        players[player_id] = {
            "id": player_id,
            "name": name,
            "nba_team": fields.get("team", "").strip() or None,
            "positions": parse_list_field(fields.get("positions")),
            "_note": filename,
            "_fantasy_teams": parse_list_field(fields.get("fantasy_teams")),
        }
    return players


class PlayerIndex:
    """Matches source cells to player objects: Yahoo link, then alias, then name."""

    def __init__(self, vault_players, aliases, report):
        self.report = report
        self.players = {}
        self.source_names = {}
        for pid, vp in vault_players.items():
            self.players[pid] = {"id": pid, "name": vp["name"], "nba_team": vp["nba_team"],
                                 "positions": vp["positions"], "in_vault": True}
        self.by_name = {}
        for pid, player in self.players.items():
            self.by_name.setdefault(normalize_name(player["name"]), []).append(pid)
        self.aliases = {}
        for entry in aliases:
            name, pid = clean_text(entry.get("source_name")), str(entry.get("player_id", ""))
            if not name or not pid or not entry.get("reason"):
                report.error("Alias entry %r needs source_name, player_id and reason." % entry)
                continue
            if pid not in self.players:
                report.error("Alias %r points to unknown player_id %s." % (name, pid))
                continue
            self.aliases[name] = pid
        self.alias_uses = set()

    def match(self, name, link, where):
        """Return a player id. Unmatched names get a stable 'u-' record."""
        pid = None
        if link:
            found = YAHOO_PLAYER_LINK.search(link)
            if found:
                pid = found.group(1)
                if pid not in self.players:
                    self.players[pid] = {"id": pid, "name": name, "nba_team": None,
                                         "positions": [], "in_vault": False}
                    self.report.warn("%s: Yahoo player %s (%s) has no vault note." % (where, pid, name))
                elif normalize_name(self.players[pid]["name"]) != normalize_name(name):
                    self.report.warn("%s: source name %r differs from vault name %r for Yahoo player %s "
                                     "(matched by link)." % (where, name, self.players[pid]["name"], pid))
        if pid is None and name in self.aliases:
            pid = self.aliases[name]
            self.alias_uses.add(name)
            self.report.warn("%s: %r matched to %s (%s) by explicit alias." % (
                where, name, self.players[pid]["name"], pid))
        if pid is None:
            candidates = self.by_name.get(normalize_name(name), [])
            if len(candidates) == 1:
                pid = candidates[0]
            elif len(candidates) > 1:
                self.report.warn("%s: %r is ambiguous (vault players %s); kept unmatched." % (
                    where, name, ", ".join(sorted(candidates))))
        if pid is None:
            pid = "u-" + (normalize_name(name) or "unknown")
            if pid not in self.players:
                self.players[pid] = {"id": pid, "name": name, "nba_team": None,
                                     "positions": [], "in_vault": False}
                self.report.warn("%s: %r has no vault match; created unmatched record %s." % (where, name, pid))
        if name != self.players[pid]["name"]:
            self.source_names.setdefault(pid, set()).add(name)
        return pid


# --------------------------------------------------------------------------
# Workbook reading

class Sheet:
    """A worksheet opened twice: formulas (with links and fills) and saved values."""

    def __init__(self, ws_formulas, ws_values):
        self.f, self.v = ws_formulas, ws_values

    def value(self, row, col):
        """Return (value, problem). Formula cells use Excel's saved result."""
        raw = self.f.cell(row=row, column=col).value
        if isinstance(raw, str) and raw.startswith("="):
            saved = self.v.cell(row=row, column=col).value
            if saved is None:
                return None, "formula %s has no saved result (open and save it in Excel)" % raw
            return saved, None
        return raw, None

    def link(self, row, col):
        link = self.f.cell(row=row, column=col).hyperlink
        return link.target if link is not None and link.target else None

    def fill(self, row, col):
        fill = self.f.cell(row=row, column=col).fill
        if fill is None or fill.fill_type != "solid" or fill.fgColor is None:
            return None
        return fill.fgColor.rgb if fill.fgColor.type == "rgb" else None

    def fill_at(self, coordinate):
        cell = self.f[coordinate]
        return self.fill(cell.row, cell.column)


def header_columns(sheet, header_row, columns, stats, report):
    """Map field -> column index from the header row. Missing headers are fatal."""
    found = {}
    for col in range(1, sheet.f.max_column + 1):
        text = clean_text(sheet.f.cell(row=header_row, column=col).value)
        if text:
            found.setdefault(text.lower(), col)
    mapping = {}
    for field, header in columns.items():
        col = found.get(header.lower())
        if col is None:
            raise ImportError_("%s / %s: header %r not found in row %d" % (
                stats.workbook, stats.sheet, header, header_row))
        mapping[field] = col
    return mapping


def row_is_blank(sheet, row, cols):
    for col in cols:
        value = sheet.f.cell(row=row, column=col).value
        if value is not None and clean_text(value) != "":
            return False
    return True


def read_fee_legend(sheet, config, stats, report):
    legend = {}
    for coordinate in config.get("legend_cells", []):
        rgb = sheet.fill_at(coordinate)
        label = clean_text(sheet.f[coordinate].value)
        if not rgb or not label:
            raise ImportError_("%s / %s: legend cell %s needs both a fill and text" % (
                stats.workbook, stats.sheet, coordinate))
        legend[rgb] = label
    legend.update(config.get("extra_fills", {}))
    display = config.get("display")
    if display is not None:
        unmapped = sorted(set(legend.values()) - set(display))
        if unmapped:
            raise ImportError_("%s / %s: fee labels %s have no entry in fee_type.display" % (
                stats.workbook, stats.sheet, unmapped))
    return legend


def fee_display(config, source_label):
    """(key, label, short) shown on the site for a legend label from the sheet."""
    entry = (config.get("display") or {}).get(source_label)
    if entry is None:
        return None, source_label, source_label
    return entry["key"], entry["label"], entry.get("short", entry["label"])


def read_draft_sheet(sheet, config, stats, players, report):
    header_row = int(config.get("header_row", 1))
    cols = header_columns(sheet, header_row, config["columns"], stats, report)
    season = normalize_season(config["season"])
    text_values = config.get("text_values", {})
    fee_cfg = config.get("fee_type")
    legend = read_fee_legend(sheet, fee_cfg, stats, report) if fee_cfg else {}
    ignored = set(config.get("ignore_cells", []))
    mapped_cols = sorted(set(cols.values()))
    _check_unmapped_cells(sheet, header_row, mapped_cols, ignored, stats, report)
    header_texts = {clean_text(h).lower() for h in config["columns"].values()}
    records = []
    for row in range(header_row + 1, sheet.f.max_row + 1):
        stats.rows_seen += 1
        if row_is_blank(sheet, row, mapped_cols):
            stats.skip(row, "blank row")
            continue
        values, problems = {}, []
        for field, col in cols.items():
            value, problem = sheet.value(row, col)
            values[field] = value
            if problem:
                problems.append("%s: %s" % (config["columns"][field], problem))
        if clean_text(values.get("player")).lower() in header_texts:
            stats.skip(row, "repeated header row")
            continue
        if clean_text(values.get("player")).lower() in ("total", "totals") or (
                not clean_text(values.get("player")) and clean_text(values.get("team")).lower() in ("total", "totals")):
            stats.skip(row, "totals row")
            continue
        if problems:
            stats.reject(row, "; ".join(problems), report)
            continue
        raw_name = clean_text(values.get("player"))
        team = clean_text(values.get("team"))
        required = [("player", raw_name)] + ([("team", team)] if "team" in cols else [])
        missing = [config["columns"][f] for f, v in required if not v]
        if missing:
            stats.reject(row, "missing required " + ", ".join(missing), report)
            continue
        pick, problem = parse_pick(values.get("pick"))
        if problem:
            stats.reject(row, problem, report)
            continue
        money, problem = {}, None
        for field in MONEY_FIELDS["draft"]:
            if field in cols:
                amount, display, note, problem = parse_money(values.get(field), text_values)
                if problem:
                    problem = "%s: %s" % (config["columns"][field], problem)
                    break
                money.update({field: amount, field + "_display": display, field + "_note": note})
        if problem:
            stats.reject(row, problem, report)
            continue
        name, nba_team, positions = raw_name, None, []
        if config.get("player_name_format") == "name_nba_team_positions":
            parsed = NAME_TEAM_POS.match(raw_name)
            if parsed:
                name = parsed.group("name").strip()
                nba_team = parsed.group("team").upper()
                positions = [p.strip() for p in parsed.group("pos").split(",") if p.strip()]
            else:
                report.warn("%s / %s row %d: player cell %r is not 'Name (TEAM - POS)'." % (
                    stats.workbook, stats.sheet, row, raw_name))
        where = "%s / %s row %d" % (stats.workbook, stats.sheet, row)
        pid = players.match(name, sheet.link(row, cols["player"]), where)
        record = {
            "season": season, "pick": pick, "player_id": pid, "source_name": name,
            "team": team or None, "nba_team": nba_team, "positions": positions,
            "_source": {"file": stats.workbook, "sheet": stats.sheet, "row": row},
        }
        for field in MONEY_FIELDS["draft"]:
            for suffix in ("", "_display", "_note"):
                record[field + suffix] = money.get(field + suffix)
        if fee_cfg:
            rgb = sheet.fill(row, cols[fee_cfg["column"]])
            source_label = legend.get(rgb) if rgb else None
            record["_fee_source"] = source_label
            record["fee_key"], record["fee_type"], record["fee_short"] = (
                fee_display(fee_cfg, source_label) if source_label else (None, None, None))
            if source_label is None:
                report.warn("%s: %s has no recognised keeper-fee colour." % (where, name))
        records.append(record)
        stats.imported += 1
    return records


def read_keeper_sheet(sheet, config, stats, players, drafts, report):
    header_row = int(config.get("header_row", 1))
    cols = header_columns(sheet, header_row, config["columns"], stats, report)
    season = normalize_season(config["season"])
    text_values = config.get("text_values", {})
    mapped_cols = sorted(set(cols.values()))
    _check_unmapped_cells(sheet, header_row, mapped_cols, set(config.get("ignore_cells", [])), stats, report)
    team_season = normalize_season(config["team_from_draft"]) if config.get("team_from_draft") else None
    header_texts = {clean_text(h).lower() for h in config["columns"].values()}
    records = []
    for row in range(header_row + 1, sheet.f.max_row + 1):
        stats.rows_seen += 1
        if row_is_blank(sheet, row, mapped_cols):
            stats.skip(row, "blank row")
            continue
        values, problems = {}, []
        for field, col in cols.items():
            value, problem = sheet.value(row, col)
            values[field] = value
            if problem:
                problems.append("%s: %s" % (config["columns"][field], problem))
        name = clean_text(values.get("player"))
        if name.lower() in header_texts:
            stats.skip(row, "repeated header row")
            continue
        if name.lower() in ("total", "totals"):
            stats.skip(row, "totals row")
            continue
        if problems:
            stats.reject(row, "; ".join(problems), report)
            continue
        if not name:
            stats.reject(row, "missing required %s" % config["columns"]["player"], report)
            continue
        money, problem = {}, None
        for field in MONEY_FIELDS["keeper"]:
            if field in cols:
                amount, display, note, problem = parse_money(values.get(field), text_values)
                if problem:
                    problem = "%s: %s" % (config["columns"][field], problem)
                    break
                money.update({field: amount, field + "_display": display, field + "_note": note})
        if problem:
            stats.reject(row, problem, report)
            continue
        where = "%s / %s row %d" % (stats.workbook, stats.sheet, row)
        link = sheet.link(row, cols["player"])
        pid = players.match(name, link, where)
        team = clean_text(values.get("team")) if "team" in cols else ""
        draft_pick = None
        if not team and team_season:
            found = [d for d in drafts if d["season"] == team_season and d["player_id"] == pid]
            if len(found) != 1:
                stats.reject(row, "expected one %s draft record for %s to supply the team, found %d" % (
                    team_season, name, len(found)), report)
                continue
            team, draft_pick = found[0]["team"], found[0]["pick"]
            # Cross-check, and fill fields the keeper sheet lacks, from the same draft row.
            for kfield, dfield in (("cost", "salary"), ("next_cost", "next_price")):
                if kfield in money:
                    if dfield in found[0] and found[0][dfield + "_display"] is not None and \
                            (found[0][dfield], found[0][dfield + "_display"]) != (money[kfield], money[kfield + "_display"]):
                        report.warn("%s: %s %s differs from the %s draft row's %s %s for %s." % (
                            where, config["columns"][kfield], money[kfield + "_display"], team_season, dfield,
                            found[0][dfield + "_display"], name))
                elif kfield in config.get("from_draft", []):
                    # e.g. the price a keeper was kept at is his price in that draft.
                    for suffix in ("", "_display", "_note"):
                        money[kfield + suffix] = found[0].get(dfield + suffix)
        if not team:
            stats.reject(row, "no team supplied", report)
            continue
        record = {"season": season, "player_id": pid, "source_name": name, "team": team,
                  "draft_pick": draft_pick, "_source": {"file": stats.workbook, "sheet": stats.sheet, "row": row}}
        for field in MONEY_FIELDS["keeper"]:
            for suffix in ("", "_display", "_note"):
                record[field + suffix] = money.get(field + suffix)
        records.append(record)
        stats.imported += 1
    return records


def read_roster_sheet(sheet, config, stats, players, report):
    """Roster snapshot. Layout 'table' (team and player columns) or 'team_blocks'
    (a team name with a 'Player' header under it, players listed below)."""
    season = normalize_season(config["season"])
    snapshot_date = clean_text(config.get("snapshot_date"))
    if not DATE.match(snapshot_date):
        raise ImportError_("%s / %s: roster snapshot needs snapshot_date YYYY-MM-DD" % (
            stats.workbook, stats.sheet))
    entries = []
    layout = config.get("layout")
    if layout == "table":
        header_row = int(config.get("header_row", 1))
        cols = header_columns(sheet, header_row, config["columns"], stats, report)
        for row in range(header_row + 1, sheet.f.max_row + 1):
            stats.rows_seen += 1
            if row_is_blank(sheet, row, sorted(set(cols.values()))):
                stats.skip(row, "blank row")
                continue
            team, p1 = sheet.value(row, cols["team"])
            name, p2 = sheet.value(row, cols["player"])
            if p1 or p2:
                stats.reject(row, p1 or p2, report)
                continue
            team, name = clean_text(team), clean_text(name)
            if not team or not name:
                stats.reject(row, "missing team or player", report)
                continue
            where = "%s / %s row %d" % (stats.workbook, stats.sheet, row)
            entries.append({"team": team, "player_id": players.match(name, sheet.link(row, cols["player"]), where),
                            "source_name": name, "_source": {"file": stats.workbook, "sheet": stats.sheet, "row": row}})
            stats.imported += 1
    elif layout == "team_blocks":
        label = clean_text(config.get("player_header", "Player")).lower()
        # Fix the bounds first: reading a cell past the edge makes openpyxl grow the sheet.
        # Counts are per non-blank cell, because each row holds several teams.
        max_row, max_col = sheet.f.max_row, sheet.f.max_column
        used = set()

        def text_at(r, c):
            return clean_text(sheet.f.cell(row=r, column=c).value) if r <= max_row else ""
        for col in range(1, max_col + 1):
            row = 1
            while row <= max_row:
                team = text_at(row, col)
                below = text_at(row + 1, col).lower()
                if team and below == label:
                    used.update(((row, col), (row + 1, col)))
                    stats.skip(row, "team name and header cells")
                    stats.skip(row + 1, "team name and header cells")
                    r = row + 2
                    while r <= max_row:
                        value, problem = sheet.value(r, col)
                        name = clean_text(value)
                        if not name:
                            break
                        used.add((r, col))
                        if problem:
                            stats.reject(r, problem, report)
                        else:
                            where = "%s / %s %s%d" % (stats.workbook, stats.sheet, _col_letter(col), r)
                            entries.append({"team": team, "player_id": players.match(name, sheet.link(r, col), where),
                                            "source_name": name,
                                            "_source": {"file": stats.workbook, "sheet": stats.sheet, "row": r}})
                            stats.imported += 1
                        r += 1
                    row = r
                else:
                    row += 1
        for row in range(1, max_row + 1):
            for col in range(1, max_col + 1):
                value = clean_text(sheet.f.cell(row=row, column=col).value)
                if not value:
                    continue
                stats.rows_seen += 1
                if (row, col) not in used:
                    stats.skip(row, "outside any team block")
                    report.warn("%s / %s %s%d: %r is outside any team block and was not imported." % (
                        stats.workbook, stats.sheet, _col_letter(col), row, value))
    else:
        raise ImportError_("%s / %s: unknown roster layout %r" % (stats.workbook, stats.sheet, layout))
    return {"season": season, "snapshot_date": snapshot_date, "source": stats.workbook, "entries": entries}


def merge_draft_prices(rows, drafts, stats, report):
    """Add prices from a second sheet (same season and pick, same player) to draft records."""
    by_pick = {(d["season"], d["pick"]): d for d in drafts if d["pick"] is not None}
    for row in rows:
        src = row["_source"]
        target = by_pick.get((row["season"], row["pick"]))
        problem = None
        if target is None:
            problem = "no %s draft pick %s to attach this price to" % (row["season"], row["pick"])
        elif target["player_id"] != row["player_id"]:
            problem = "pick %s is %s here but %s in the draft sheet" % (
                row["pick"], row["source_name"], target["source_name"])
        else:
            for field in MONEY_FIELDS["draft"]:
                if row.get(field + "_display") is None:
                    continue
                if target.get(field + "_display") is not None and \
                        (target[field], target[field + "_display"]) != (row[field], row[field + "_display"]):
                    problem = "%s %s conflicts with %s in the draft sheet" % (
                        field, row[field + "_display"], target[field + "_display"])
                    break
        if problem:
            stats.imported -= 1
            stats.reject(src["row"], problem, report)
            continue
        for field in MONEY_FIELDS["draft"]:
            if row.get(field + "_display") is not None:
                for suffix in ("", "_display", "_note"):
                    target[field + suffix] = row[field + suffix]
                target.setdefault("_price_sources", []).append(src)


def price_roster(snap, cfg, drafts, report):
    """Attach the recorded next-season keeper price to each rostered player."""
    season = cfg.get("keeper_prices_from_draft")
    if not season:
        return
    season = normalize_season(season)
    undrafted = cfg.get("undrafted_price")
    if undrafted and (not isinstance(undrafted.get("amount"), (int, float)) or not undrafted.get("basis")):
        raise ImportError_("undrafted_price needs a numeric amount and a basis")
    by_player = {}
    for d in drafts:
        if d["season"] == season:
            by_player.setdefault(d["player_id"], []).append(d)
    snap["price_from_draft"] = season
    snap["undrafted_rule"] = undrafted["basis"] if undrafted else None
    snap["undrafted_price"] = undrafted["amount"] if undrafted else None
    for e in snap["entries"]:
        found = by_player.get(e["player_id"], [])
        if len(found) > 1:
            report.warn("Roster %s: %s has %d %s draft records; no price shown." % (
                snap["season"], e["source_name"], len(found), season))
            found = []
        if found:
            d = found[0]
            e.update({"keeper_price": d.get("next_price"), "keeper_price_display": d.get("next_price_display"),
                      "keeper_price_note": d.get("next_price_note"), "price_basis": "drafted",
                      "drafted_by": d["team"], "draft_pick": d["pick"],
                      "fee_key": d.get("fee_key"), "fee_type": d.get("fee_type"), "fee_short": d.get("fee_short")})
            if d.get("next_price_display") is None:
                report.warn("Roster %s: %s was drafted in %s but has no recorded keeper price." % (
                    snap["season"], e["source_name"], season))
        elif undrafted:
            amount = undrafted["amount"]
            amount = int(amount) if float(amount).is_integer() else amount
            e.update({"keeper_price": amount, "keeper_price_display": "$%s" % amount,
                      "keeper_price_note": None, "price_basis": "undrafted", "drafted_by": None, "draft_pick": None,
                      "fee_key": undrafted.get("fee_key"), "fee_type": undrafted.get("fee_label"),
                      "fee_short": undrafted.get("fee_short")})
        else:
            e.update({"keeper_price": None, "keeper_price_display": None, "keeper_price_note": None,
                      "price_basis": None, "drafted_by": None, "draft_pick": None,
                      "fee_key": None, "fee_type": None, "fee_short": None})


def check_accuracy(drafts, cfg, report):
    """Report-only check of recorded keeper prices against the configured fee rules.
    Nothing here is published; it tells the owner where the workbook disagrees."""
    if not cfg:
        return None
    season = normalize_season(cfg["season"])
    rules, skip = cfg.get("rules", {}), set(cfg.get("skip", []))
    result = {"season": season, "checked": 0, "matched": 0, "mismatches": [], "rounding": [], "skipped": [],
              "not_checked": []}
    for d in drafts:
        if d["season"] != season:
            continue
        label = d.get("fee_key")
        if label in skip:
            result["skipped"].append((d["pick"], d["source_name"], d.get("salary_display"), d.get("next_price_display"), d.get("fee_type")))
            continue
        rule = rules.get(label)
        if rule is None or d.get("salary") is None or d.get("next_price") is None:
            result["not_checked"].append((d["pick"], d["source_name"], d.get("salary_display"),
                                          d.get("next_price_display"), d.get("_fee_source") or "no fee colour"))
            continue
        result["checked"] += 1
        price = d["salary"]
        band_ok = (rule.get("salary_below") is None or price < rule["salary_below"]) and \
                  (rule.get("salary_at_least") is None or price >= rule["salary_at_least"])
        base = max(price, rule.get("min_price", 0))
        # League rounding (owner, 2026-10-09): whole dollars, nearest dollar, 50 cents rounds up.
        # Integer maths avoids float error: 10% of $55 is $5.50, which becomes $6.
        tenths = base * rule.get("pct", 0)          # fee in tenths of a dollar when pct is 10
        fee_dollars = (tenths + 50) // 100
        expected = base + max(fee_dollars, rule.get("min_fee", 0))
        options = [expected]
        if tenths % 100 and tenths > rule.get("min_fee", 0) * 100:  # rounding decided the fee
            result["rounding"].append((d["pick"], d["source_name"], d["salary_display"], d["next_price_display"],
                                       "%s%% is $%.2f, rounded to $%s" % (rule.get("pct", 0), tenths / 100.0, fee_dollars)))
        if d["next_price"] in options and band_ok:
            result["matched"] += 1
        else:
            why = "expected %s" % " or ".join("$%s" % o for o in options)
            if not band_ok:
                why = "fee colour does not fit a $%s draft price; %s" % (price, why)
            result["mismatches"].append((d["pick"], d["source_name"], d["salary_display"], d["next_price_display"],
                                         d.get("_fee_source") or label, why))
    return result


def _col_letter(col):
    letters = ""
    while col:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _check_unmapped_cells(sheet, header_row, mapped_cols, ignored, stats, report):
    """Warn about values in columns the mapping does not read, so nothing is dropped silently."""
    for row in range(1, sheet.f.max_row + 1):
        for col in range(1, sheet.f.max_column + 1):
            if col in mapped_cols:
                continue
            coordinate = "%s%d" % (_col_letter(col), row)
            value = clean_text(sheet.f.cell(row=row, column=col).value)
            if value and coordinate not in ignored:
                report.warn("%s / %s %s: %r is in an unmapped column and was not imported." % (
                    stats.workbook, stats.sheet, coordinate, value))


# --------------------------------------------------------------------------
# Rules (Obsidian Markdown -> JSON blocks rendered with textContent on the site)

LIST_ITEM = re.compile(r"^(?:(?P<num>\d+)[.)]|(?P<bullet>[-*+]))\s+(?P<text>.*)$")


def indent_width(line):
    width = 0
    for char in line:
        if char == " ":
            width += 1
        elif char == "\t":
            width += 4 - width % 4
        else:
            break
    return width


def parse_inline(text, resolve):
    """Bold, italic, strikethrough, [[wiki links]] and \\ escapes into nodes."""
    nodes, buf, i = [], [], 0

    def flush():
        if buf:
            nodes.append({"t": "text", "v": "".join(buf)})
            del buf[:]

    while i < len(text):
        char = text[i]
        if char == "\\" and i + 1 < len(text) and not text[i + 1].isalnum():
            buf.append(text[i + 1])
            i += 2
            continue
        if text.startswith("[[", i):
            end = text.find("]]", i + 2)
            if end != -1:
                flush()
                inner = text[i + 2:end]
                target, _, label = inner.partition("|")
                nodes.append(resolve(target.strip(), (label or target).strip()))
                i = end + 2
                continue
        matched = False
        for delim, kind in (("**", "strong"), ("__", "strong"), ("~~", "del")):
            if text.startswith(delim, i):
                end = text.find(delim, i + 2)
                if end > i + 2:
                    flush()
                    nodes.append({"t": kind, "c": parse_inline(text[i + 2:end], resolve)})
                    i = end + 2
                    matched = True
                break
        if matched:
            continue
        if char in "*_" and i + 1 < len(text) and text[i + 1] not in (" ", char) \
                and not (char == "_" and i > 0 and text[i - 1].isalnum()):
            end = i + 1
            while True:
                end = text.find(char, end + 1)
                if end == -1:
                    break
                if text[end - 1] != " " and not text.startswith(char * 2, end) \
                        and not (char == "_" and end + 1 < len(text) and text[end + 1].isalnum()):
                    break
            if end != -1:
                flush()
                nodes.append({"t": "em", "c": parse_inline(text[i + 1:end], resolve)})
                i = end + 1
                continue
        buf.append(char)
        i += 1
    flush()
    return nodes


def parse_rules_markdown(text, resolve):
    """Lists nest by indentation (tab = 4 spaces); source numbers are kept."""
    text = text.replace("\r\n", "\n")
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            text = text[end + 4:].lstrip("\n")
    blocks = []
    stack = []          # [{"indent", "list", "item"}]
    last_para = None    # paragraph that a following line continues
    for line in text.split("\n"):
        if not line.strip():
            last_para = None
            continue
        indent = indent_width(line)
        body = line.strip()
        item = LIST_ITEM.match(body)
        if indent == 0 and re.match(r"^#{1,6}\s", body):
            stack, last_para = [], None
            level = len(body) - len(body.lstrip("#"))
            blocks.append({"t": "heading", "level": level, "c": parse_inline(body[level:].strip(), resolve)})
            continue
        if indent == 0 and re.match(r"^(-{3,}|\*{3,}|_{3,})$", body):
            stack, last_para = [], None
            blocks.append({"t": "hr"})
            continue
        if item:
            ordered = item.group("num") is not None
            while stack and stack[-1]["indent"] > indent:
                stack.pop()
            if stack and stack[-1]["indent"] == indent and stack[-1]["list"]["ordered"] != ordered:
                stack.pop()
            new_item = {"c": parse_inline(item.group("text"), resolve), "blocks": []}
            if ordered:
                new_item["n"] = int(item.group("num"))
            if stack and stack[-1]["indent"] == indent:
                stack[-1]["list"]["items"].append(new_item)
                stack[-1]["item"] = new_item
            else:
                new_list = {"t": "list", "ordered": ordered, "items": [new_item]}
                (stack[-1]["item"]["blocks"] if stack else blocks).append(new_list)
                stack.append({"indent": indent, "list": new_list, "item": new_item})
            last_para = None
            continue
        owners = [entry for entry in stack if entry["indent"] < indent]
        if owners:
            owner = owners[-1]
            del stack[stack.index(owner) + 1:]
            if last_para is not None and last_para in owner["item"]["blocks"]:
                last_para["c"].extend([{"t": "text", "v": " "}] + parse_inline(body, resolve))
            else:
                last_para = {"t": "p", "c": parse_inline(body, resolve)}
                owner["item"]["blocks"].append(last_para)
            continue
        stack = []
        if last_para is not None and blocks and blocks[-1] is last_para:
            last_para["c"].extend([{"t": "text", "v": " "}] + parse_inline(body, resolve))
        else:
            last_para = {"t": "p", "c": parse_inline(body, resolve)}
            blocks.append(last_para)
    return blocks


def build_rules(manifest, vault, report):
    docs_cfg = manifest.get("rules", [])
    by_stem = {}
    for doc in docs_cfg:
        by_stem[os.path.splitext(os.path.basename(doc["file"]))[0].lower()] = doc["id"]
    link_cfg = {k.lower(): v for k, v in manifest.get("rules_links", {}).items()}
    unresolved = []

    def resolve(target, label):
        page, _, heading = target.partition("#")
        key = page.strip().lower()
        if key in by_stem:
            return {"t": "link", "doc": by_stem[key], "c": [{"t": "text", "v": label}]}
        if (link_cfg.get(key) or {}).get("section"):
            # A vault note replaced by a section of the site, e.g. Rules Amendments -> suggestion form.
            return {"t": "link", "section": link_cfg[key]["section"], "c": [{"t": "text", "v": label}]}
        reason = (link_cfg.get(key) or {}).get("unavailable", "not part of this archive")
        unresolved.append((target, reason))
        return {"t": "missing", "v": label, "note": reason}

    docs = []
    for doc in docs_cfg:
        path = os.path.join(vault, doc["file"])
        if not os.path.isfile(path):
            raise ImportError_("rules file not found in vault: %s" % doc["file"])
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        if not text.strip():
            report.error("Rules file %s is empty." % doc["file"])
            continue
        docs.append({"id": doc["id"], "title": doc["title"], "status": doc["status"],
                     "source": os.path.basename(doc["file"]), "blocks": parse_rules_markdown(text, resolve)})
    for target, reason in sorted(set(unresolved)):
        report.warn("Rules link [[%s]] has no page on the site; shown as unavailable (%s)." % (target, reason))
    for entry in manifest.get("rules_excluded", []):
        report.notes.append("Rules file %s not published: %s" % (entry["file"], entry["reason"]))
    return docs


# --------------------------------------------------------------------------
# Assembly and validation

WIKI_LINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")


def build_league(manifest, vault, report):
    """League name and home-page facts. Facts are 'Label: value' lines read from a vault
    note (only the labels listed in the manifest), so the vault stays the one source."""
    cfg = manifest.get("league") or {}
    league = {"name": cfg.get("name", "League archive"), "short_name": cfg.get("short_name"), "facts": []}
    note = cfg.get("home_note")
    if not note:
        return league
    path = os.path.join(vault, note)
    if not os.path.isfile(path):
        raise ImportError_("league home note not found in vault: %s" % note)
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    found = {}
    for line in lines:
        key, sep, value = line.partition(":")
        if sep and key.strip() in cfg.get("facts", []):
            value = WIKI_LINK.sub(lambda m: (m.group(2) or m.group(1)).strip(), value).strip()
            found[key.strip()] = value
    people = set((manifest.get("people") or {}).get("league_facts", []))
    for key in cfg.get("facts", []):
        if found.get(key):
            value = short_person(found[key]) if key in people else found[key]
            league["facts"].append({"label": key, "value": value})
        else:
            report.warn("League note %s has no '%s:' line; not shown on the home page." % (note, key))
    return league


ISO_WITH_OFFSET = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?([+-]\d{2}:\d{2}|Z)$")


def build_events(manifest):
    """Dated league events (countdowns) and home-page announcements from the manifest.
    Times must carry a UTC offset so every visitor counts down to the same moment."""
    events = []
    for e in manifest.get("events", []):
        if not e.get("id") or not e.get("label") or not ISO_WITH_OFFSET.match(e.get("at", "")):
            raise ImportError_("events need id, label and 'at' as ISO time with offset, e.g. 2026-10-14T19:00:00-04:00")
        events.append({"id": e["id"], "label": e["label"], "at": e["at"], "zone": e.get("zone"),
                       "passed_text": e.get("passed_text")})
    ids = {e["id"] for e in events}
    notes = []
    for a in manifest.get("announcements", []):
        if not a.get("text"):
            raise ImportError_("announcements need text")
        if a.get("until_event") and a["until_event"] not in ids:
            raise ImportError_("announcement until_event %r is not an event id" % a["until_event"])
        notes.append({"text": a["text"], "until_event": a.get("until_event")})
    return events, notes


GOOGLE_FORM = re.compile(r"^https://(docs\.google\.com/forms/|forms\.gle/)[A-Za-z0-9_\-/?=&.]+$")


def build_suggestions(manifest):
    """Rule-change suggestions are collected by an external form the owner runs (Google Forms);
    the site only links to it, and never sees submissions."""
    cfg = manifest.get("suggestions")
    if not cfg:
        return None
    url = cfg.get("form_url") or None
    if url and not GOOGLE_FORM.match(url):
        raise ImportError_("suggestions.form_url must be a Google Forms link (https://docs.google.com/forms/... "
                           "or https://forms.gle/...)")
    return {"title": cfg.get("title", "Suggest a rule change"), "season": cfg.get("season"),
            "intro": cfg.get("intro", ""), "form_url": url}


def load_json(path, label):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise ImportError_("%s not found: %s" % (label, os.path.relpath(path, ROOT)))
    except ValueError as exc:
        raise ImportError_("%s is not valid JSON: %s" % (label, exc))


def resolve_path(path):
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(ROOT, path))


def open_workbook(path):
    try:
        import openpyxl
    except ImportError:
        raise ImportError_("openpyxl is not installed; run: python3 -m pip install -r requirements-archive.txt")
    try:
        return openpyxl.load_workbook(path, data_only=False), openpyxl.load_workbook(path, data_only=True)
    except Exception as exc:  # corrupt or not a workbook
        raise ImportError_("cannot open %s: %s" % (os.path.basename(path), exc))


def check_duplicates(drafts, keepers, report):
    seen_pick, seen_player = {}, {}
    for d in drafts:
        if d["pick"] is not None:
            key = (d["season"], d["pick"])
            if key in seen_pick:
                report.error("%s draft pick %d appears twice (%s and %s)." % (
                    d["season"], d["pick"], _where(seen_pick[key]), _where(d)))
            seen_pick[key] = d
        key = (d["season"], d["player_id"])
        if key in seen_player:
            report.warn("Possible duplicate: %s drafted twice in %s (%s and %s)." % (
                d["source_name"], d["season"], _where(seen_player[key]), _where(d)))
        seen_player[key] = d
    seen = {}
    for k in keepers:
        key = (k["season"], k["player_id"])
        if key in seen:
            report.error("%s is kept twice in %s (%s and %s)." % (
                k["source_name"], k["season"], _where(seen[key]), _where(k)))
        seen[key] = k


def _where(record):
    src = record["_source"]
    return "%s row %d" % (src["sheet"], src["row"])


def check_integrity(data, report):
    ids = {p["id"] for p in data["players"]}
    problems = 0
    for kind in ("drafts", "keepers"):
        for record in data[kind]:
            if record["player_id"] not in ids:
                problems += 1
                report.error("%s record %s points to missing player %s." % (kind, record["id"], record["player_id"]))
    for snap in data["rosters"]:
        for entry in snap["entries"]:
            if entry["player_id"] not in ids:
                problems += 1
                report.error("Roster %s points to missing player %s." % (snap["id"], entry["player_id"]))
    team_ids = {t["id"] for t in data["teams"]}
    for record in data["drafts"] + data["keepers"] + [e for s in data["rosters"] for e in s["entries"]]:
        if record.get("team_id") not in team_ids:
            problems += 1
            report.error("Record for %s points to missing team %s." % (record.get("source_name"), record.get("team_id")))
    record_ids = [r["id"] for r in data["drafts"] + data["keepers"]] + [s["id"] for s in data["rosters"]]
    if len(record_ids) != len(set(record_ids)):
        problems += 1
        report.error("Record ids are not unique.")
    return problems


def build(manifest_path, aliases_path, output_path, vault_override=None):
    report = Report()
    manifest = load_json(manifest_path, "manifest")
    aliases = load_json(aliases_path, "aliases").get("aliases", [])
    vault = resolve_path(vault_override or manifest["vault"])
    if not os.path.isdir(vault):
        raise ImportError_("vault folder not found: %s" % (vault_override or manifest["vault"]))
    vault_players = load_vault_players(os.path.join(vault, manifest.get("vault_players_dir", "Silmaril/players")), report)
    players = PlayerIndex(vault_players, aliases, report)
    workbooks_dir = resolve_path(manifest.get("workbooks_dir", "archive/workbooks"))

    drafts, keepers, rosters, sources, fee_types = [], [], [], [], {}
    pending_keepers, pending_prices, roster_cfgs = [], [], []
    for wb_cfg in manifest.get("workbooks", []) + [{"file": r["file"], "sheets": [r]} for r in manifest.get("rosters", [])]:
        path = os.path.join(workbooks_dir, wb_cfg["file"])
        if not os.path.isfile(path):
            raise ImportError_("workbook not found: %s (place it in %s)" % (
                wb_cfg["file"], os.path.relpath(workbooks_dir, ROOT)))
        wb_f, wb_v = open_workbook(path)
        listed = {s["sheet"] for s in wb_cfg["sheets"]}
        if not any(s.get("kind") == "roster" for s in wb_cfg["sheets"]):
            unlisted = [name for name in wb_f.sheetnames if name not in listed]
            if unlisted:
                raise ImportError_("%s: sheets %s are not in the manifest; map or ignore them" % (
                    wb_cfg["file"], ", ".join(repr(n) for n in unlisted)))
        published_sheets = []
        for cfg in wb_cfg["sheets"]:
            if cfg["sheet"] not in wb_f.sheetnames:
                raise ImportError_("%s: sheet %r not found" % (wb_cfg["file"], cfg["sheet"]))
            stats = SheetStats(wb_cfg["file"], cfg["sheet"], cfg.get("kind", "roster"))
            stats.note = cfg.get("_comment", "")
            report.sheets.append(stats)
            sheet = Sheet(wb_f[cfg["sheet"]], wb_v[cfg["sheet"]])
            kind = cfg.get("kind", "roster")
            if kind == "ignore":
                stats.note = "Not imported: " + cfg.get("reason", "no reason given")
                stats.rows_seen = sheet.f.max_row
                continue
            if kind == "draft":
                drafts.extend(read_draft_sheet(sheet, cfg, stats, players, report))
                if cfg.get("fee_type"):
                    fee_types[normalize_season(cfg["season"])] = {
                        "applies_to": normalize_season(cfg["fee_type"]["season"]),
                        "labels": [dict(zip(("key", "label", "short", "source"),
                                            fee_display(cfg["fee_type"], label) + (label,)))
                                   for label in sorted(set(read_fee_legend(sheet, cfg["fee_type"], stats, report).values()))],
                    }
            elif kind == "draft_prices":
                pending_prices.append((sheet, cfg, stats))
            elif kind == "keeper":
                pending_keepers.append((sheet, cfg, stats))
            elif kind == "roster":
                snap = read_roster_sheet(sheet, cfg, stats, players, report)
                snap["label"] = clean_text(cfg.get("label")) or None
                rosters.append(snap)
                roster_cfgs.append((snap, cfg))
            else:
                raise ImportError_("%s / %s: unknown kind %r" % (wb_cfg["file"], cfg["sheet"], kind))
            published_sheets.append(cfg["sheet"])
        if published_sheets:
            sources.append({"file": wb_cfg["file"], "sheets": published_sheets})
    # Extra draft prices, then keepers, are read after all drafts: both attach to draft records.
    for sheet, cfg, stats in pending_prices:
        merge_draft_prices(read_draft_sheet(sheet, cfg, stats, players, report), drafts, stats, report)
    for season in sorted({d["season"] for d in drafts}, key=season_key):
        missing = [d["source_name"] for d in drafts if d["season"] == season and d.get("salary_display") is None]
        if missing and pending_prices:
            report.warn("%s: %d draft records have no draft price: %s" % (season, len(missing), ", ".join(missing[:10])))
    for sheet, cfg, stats in pending_keepers:
        keepers.extend(read_keeper_sheet(sheet, cfg, stats, players, drafts, report))

    for snap, cfg in roster_cfgs:
        price_roster(snap, cfg, drafts, report)
    report.accuracy = check_accuracy(drafts, manifest.get("accuracy_check"), report)

    kept = {(k["season"], k["player_id"]) for k in keepers}
    drafts.sort(key=lambda d: (season_key(d["season"]), d["pick"] if d["pick"] is not None else 10 ** 6, d["source_name"]))
    for d in drafts:
        d["id"] = "d-%s-%s" % (d["season"], "%03d" % d["pick"] if d["pick"] is not None else normalize_name(d["source_name"]))
        d["kept"] = (d["season"], d["player_id"]) in kept
    keepers.sort(key=lambda k: (season_key(k["season"]), k["team"].lower(), k["source_name"]))
    for k in keepers:
        k["id"] = "k-%s-%s" % (k["season"], k["player_id"])
    rosters.sort(key=lambda s: (season_key(s["season"]), s["snapshot_date"]))
    for snap in rosters:
        snap["id"] = "r-%s-%s" % (snap["season"], snap["snapshot_date"])
        snap["entries"].sort(key=lambda e: (e["team"].lower(), e["source_name"]))
    check_duplicates(drafts, keepers, report)

    # The workbook's "(TEAM - POS)" suffix and the vault note may describe different dates.
    for d in drafts:
        vp = vault_players.get(d["player_id"])
        if vp and d["nba_team"] and (d["nba_team"] != vp["nba_team"] or sorted(d["positions"]) != sorted(vp["positions"])):
            report.disagreements.append((d["source_name"], "%s %s" % (d["nba_team"], ",".join(d["positions"])),
                                         "%s %s" % (vp["nba_team"], ",".join(vp["positions"])), _where(d)))

    for pid, names in players.source_names.items():
        players.players[pid]["aliases"] = sorted(names)
    referenced = {r["player_id"] for r in drafts + keepers} | {e["player_id"] for s in rosters for e in s["entries"]}
    unreferenced_vault = sum(1 for pid in vault_players if pid not in referenced)
    if any(vp["_fantasy_teams"] for vp in vault_players.values()):
        report.notes.append("Some vault notes list fantasy_teams; that field is not published (rosters come from imports).")

    # Takeovers: from a given season on, a roster listed under the old team name belongs to
    # the new team. Earlier drafts and keepers keep the old (historical) name.
    successions = []
    for entry in manifest.get("team_successions", []):
        old_name, new_name = clean_text(entry.get("from")), clean_text(entry.get("to"))
        start = normalize_season(entry["from_season"])
        if not old_name or not new_name or not entry.get("note"):
            raise ImportError_("team_successions entries need from, to, from_season and note")
        moved = 0
        for snap in rosters:
            if season_key(snap["season"]) >= season_key(start):
                for e in snap["entries"]:
                    if e["team"] == old_name:
                        e["team"] = new_name
                        moved += 1
        successions.append({"from": old_name, "to": new_name, "season": start, "note": entry["note"]})
        report.notes.append("%s took over %s from %s: %d roster entries moved to the new team." % (
            new_name, old_name, start, moved))
    team_names = sorted({r["team"] for r in drafts + keepers} | {e["team"] for s in rosters for e in s["entries"]},
                        key=str.lower)
    by_norm = {}
    for team in team_names:
        by_norm.setdefault(normalize_name(team), []).append(team)
    for variants in by_norm.values():
        if len(variants) > 1:
            report.warn("Team names %s differ only in punctuation or case; kept separate." % variants)
    # Team objects: one per distinct team name, linked to a vault team note when one matches.
    team_cfg = manifest.get("teams") or {}
    team_cfg = dict(team_cfg, _people_fields=tuple((manifest.get("people") or {}).get("team_fields", [])))
    vault_teams = load_vault_teams(vault, team_cfg, report)
    teams, team_ids, used = [], {}, set()
    for name in team_names:
        tid = base = team_slug(name)
        n = 2
        while tid in used:
            tid = "%s-%d" % (base, n)
            n += 1
        used.add(tid)
        team_ids[name] = tid
        note = vault_teams.get(normalize_name(name))
        teams.append({"id": tid, "name": name, "in_vault": bool(note), "info": note["info"] if note else {}})
    matched = {normalize_name(t) for t in team_names}
    for key, note in sorted(vault_teams.items()):
        if key not in matched:
            report.warn("Vault team note %s (%s) matches no team in the records." % (note["note"], note["name"]))
    by_name = {t["name"]: t for t in teams}
    for succ in successions:
        old_team, new_team = by_name.get(succ["from"]), by_name.get(succ["to"])
        if not old_team or not new_team:
            report.warn("Takeover %s -> %s: one of the teams has no records; link not shown." % (succ["from"], succ["to"]))
            continue
        old_team["succeeded_by"] = {"team_id": new_team["id"], "season": succ["season"], "note": succ["note"]}
        new_team["took_over"] = {"team_id": old_team["id"], "season": succ["season"], "note": succ["note"]}
    missing_notes = [t["name"] for t in teams if not t["in_vault"]]
    if missing_notes:
        report.notes.append("Teams without a vault team note (%d): %s." % (len(missing_notes), ", ".join(missing_notes)))
    for record in drafts + keepers:
        record["team_id"] = team_ids[record["team"]]
    for snap in rosters:
        for entry in snap["entries"]:
            entry["team_id"] = team_ids[entry["team"]]
            if entry.get("drafted_by"):
                entry["drafted_by_id"] = team_ids.get(entry["drafted_by"])

    def public(record):
        return {k: v for k, v in record.items() if not k.startswith("_")}

    data = {
        "schema": SCHEMA,
        "imported_at": None,
        "seasons": {
            "draft": sorted({d["season"] for d in drafts}, key=season_key),
            "keeper": sorted({k["season"] for k in keepers}, key=season_key),
            "roster": sorted({s["season"] for s in rosters}, key=season_key),
        },
        "sources": sources,
        "fee_types": fee_types,
        "teams": teams,
        "players": sorted((public(p) for p in players.players.values()),
                          key=lambda p: (normalize_name(p["name"]), p["id"])),
        "drafts": [public(d) for d in drafts],
        "keepers": [public(k) for k in keepers],
        "rosters": [{"id": s["id"], "season": s["season"], "snapshot_date": s["snapshot_date"],
                     "label": s.get("label"), "price_from_draft": s.get("price_from_draft"),
                     "undrafted_rule": s.get("undrafted_rule"), "undrafted_price": s.get("undrafted_price"),
                     "entries": [public(e) for e in s["entries"]]} for s in rosters],
        "rules": build_rules(manifest, vault, report),
        "suggestions": build_suggestions(manifest),
        "league": build_league(manifest, vault, report),
        "events": build_events(manifest)[0],
        "announcements": build_events(manifest)[1],
    }
    check_integrity(data, report)

    # Earlier roster snapshots must survive later imports.
    previous = None
    if os.path.isfile(output_path):
        try:
            with open(output_path, encoding="utf-8") as handle:
                previous = json.load(handle)
        except ValueError:
            report.warn("Existing %s is not valid JSON; it will be replaced." % os.path.basename(output_path))
    if previous and previous.get("schema") == SCHEMA:
        kept_ids = {s["id"] for s in data["rosters"]}
        for snap in previous.get("rosters", []):
            if snap["id"] not in kept_ids:
                report.error("Roster snapshot %s is already published but missing from the manifest. "
                             "Keep its workbook and manifest entry so it is preserved." % snap["id"])

    records = {}
    for record in drafts + keepers:
        records[record["id"]] = [record["_source"]] + record.get("_price_sources", [])
    for snap in rosters:
        for entry in snap["entries"]:
            records.setdefault(snap["id"], []).append(entry["_source"])
    stats = {"vault_players": len(vault_players), "unreferenced_vault": unreferenced_vault,
             "alias_uses": sorted(players.alias_uses), "referenced": len(referenced)}
    return data, report, records, previous, stats


def write_atomic(path, text):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=directory, prefix=".archive-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def render_report(data, report, stats, status):
    lines = ["# Archive import report", "",
             "Rows are counted per row, except team-block roster sheets, which count non-blank cells.", "",
             "Generated by `import_archive.py`. Status: **%s**." % status, ""]
    if data:
        lines += ["- Import date: %s" % (data.get("imported_at") or "not published"),
                  "- Draft seasons: %s" % (", ".join(data["seasons"]["draft"]) or "none"),
                  "- Keeper seasons: %s" % (", ".join(data["seasons"]["keeper"]) or "none"),
                  "- Roster snapshots: %s" % (", ".join("%s (%s)" % (s["season"], s["snapshot_date"])
                                                         for s in data["rosters"]) or "none imported"),
                  "- Players published: %d (%d vault notes, %d referenced by records, %d not in the vault)" % (
                      len(data["players"]), stats["vault_players"], stats["referenced"],
                      sum(1 for p in data["players"] if not p["in_vault"])),
                  "- Records: %d draft, %d keeper, %d roster entries" % (
                      len(data["drafts"]), len(data["keepers"]), sum(len(s["entries"]) for s in data["rosters"])),
                  "- Rules pages: %s" % ", ".join("%s (%s)" % (d["title"], d["status"]) for d in data["rules"]),
                  ""]
    lines += ["## Sheets", "", "| Workbook / sheet | Kind | Rows read | Imported | Skipped | Rejected | Reconciles |",
              "| --- | --- | ---: | ---: | ---: | ---: | --- |"]
    for s in report.sheets:
        skipped = sum(len(v) for v in s.skipped.values())
        ok = "n/a" if s.kind == "ignore" else ("yes" if s.imported + skipped + len(s.rejected) == s.rows_seen else "NO")
        lines.append("| %s / %s | %s | %d | %d | %d | %d | %s |" % (
            s.workbook, s.sheet, s.kind, s.rows_seen, s.imported, skipped, len(s.rejected), ok))
    lines.append("")
    for s in report.sheets:
        details = []
        if s.note:
            details.append("- %s" % s.note)
        for reason, rows in sorted(s.skipped.items()):
            details.append("- Skipped (%s): %d: rows %s" % (reason, len(rows), _ranges(rows)))
        for row, reason in s.rejected:
            details.append("- Rejected row %d: %s" % (row, reason))
        if details:
            lines += ["**%s / %s**" % (s.workbook, s.sheet), ""] + details + [""]
    lines += ["## Errors (block publishing)", ""] + (["- " + e for e in report.errors] or ["None."]) + [""]
    lines += ["## Warnings (published; review)", ""] + (["- " + w for w in report.warnings] or ["None."]) + [""]
    acc = report.accuracy
    if acc:
        lines += ["## Keeper price accuracy (%s draft)" % acc["season"], "",
                  "Each recorded next-season keeper price is checked against its fee colour and the draft price, using "
                  "the rules in the manifest (`accuracy_check`). This check is only in this report; the site shows the "
                  "prices as recorded.", "",
                  "- Checked: %d. Correct: %d. Wrong: %d." % (acc["checked"], acc["matched"], len(acc["mismatches"])),
                  "- Not checked (marked CBK): %d. Not checked (missing price or colour): %d." % (
                      len(acc["skipped"]), len(acc["not_checked"])), ""]
        if acc["mismatches"]:
            lines += ["| Pick | Player | Draft price | Recorded keeper price | Fee type | Problem |",
                      "| ---: | --- | ---: | ---: | --- | --- |"]
            lines += ["| %s | %s | %s | %s | %s | %s |" % row for row in acc["mismatches"]] + [""]
        if acc["rounding"]:
            lines += ["Rounded fees (whole dollars; 50 cents and up rounds up):", ""]
            lines += ["- Pick %s %s: draft price %s, recorded %s; %s" % row
                      for row in acc["rounding"]] + [""]
        if acc["not_checked"]:
            lines += ["Not checked:", ""] + ["- Pick %s %s: draft %s, keeper %s (%s)" % row for row in acc["not_checked"]] + [""]
        if acc["skipped"]:
            lines += ["Marked CBK (Can't Be Kept), so no price to check: " + "; ".join(
                "pick %s %s (drafted at %s)" % (r[0], r[1], r[2] or "—") for r in acc["skipped"]), ""]
    if report.disagreements:
        lines += ["## Workbook vs vault: NBA team and positions", "",
                  "The draft sheet's `Name (TEAM - POS)` text and the vault note disagree for %d players. "
                  "Both are published and labelled by source; neither is treated as draft-day fact." % len(report.disagreements), "",
                  "| Player | Workbook | Vault | Source |", "| --- | --- | --- | --- |"]
        lines += ["| %s | %s | %s | %s |" % row for row in report.disagreements] + [""]
    if report.notes:
        lines += ["## Notes", ""] + ["- " + n for n in report.notes] + [""]
    return "\n".join(lines)


def _ranges(rows):
    rows = sorted(set(rows))
    out, start, prev = [], None, None
    for r in rows + [None]:
        if start is None:
            start = prev = r
        elif r is not None and r == prev + 1:
            prev = r
        else:
            out.append(str(start) if start == prev else "%d-%d" % (start, prev))
            start = prev = r
    return ", ".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="validate and write the report only")
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--aliases", default=DEFAULT_ALIASES)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--report", default=DEFAULT_REPORT)
    parser.add_argument("--records", default=DEFAULT_RECORDS)
    parser.add_argument("--vault", help="vault folder (default from the manifest)")
    parser.add_argument("--now", help=argparse.SUPPRESS)  # fixed timestamp for tests
    args = parser.parse_args(argv)

    try:
        data, report, records, previous, stats = build(args.manifest, args.aliases, args.output, args.vault)
    except (ImportError_, ValueError, KeyError) as exc:
        print("Import failed: %s" % exc, file=sys.stderr)
        print("Nothing was written; the published archive is unchanged.", file=sys.stderr)
        return 1

    if report.errors:
        write_atomic(args.report, render_report(data, report, stats, "FAILED, nothing published"))
        print("Import failed with %d error(s); the published archive is unchanged. See %s." % (
            len(report.errors), os.path.relpath(args.report, ROOT)), file=sys.stderr)
        for message in report.errors[:20]:
            print("  - " + message, file=sys.stderr)
        return 1

    def comparable(d):
        return {k: v for k, v in d.items() if k != "imported_at"}

    unchanged = previous is not None and comparable(previous) == comparable(data)
    if unchanged:
        data["imported_at"] = previous.get("imported_at")
    else:
        data["imported_at"] = args.now or datetime.datetime.now(datetime.timezone.utc).replace(
            microsecond=0).isoformat().replace("+00:00", "Z")

    status = "checked only" if args.check else ("unchanged" if unchanged else "published")
    if not args.check:
        if not unchanged:
            write_atomic(args.output, json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
        write_atomic(args.records, json.dumps(records, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    write_atomic(args.report, render_report(data, report, stats, status))
    print("%s: %d draft, %d keeper, %d roster snapshot(s), %d players; %d warning(s). Report: %s" % (
        status.capitalize(), len(data["drafts"]), len(data["keepers"]), len(data["rosters"]),
        len(data["players"]), len(report.warnings), os.path.relpath(args.report, ROOT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
