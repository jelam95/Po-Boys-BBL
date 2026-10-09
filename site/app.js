"use strict";

// All archive strings are inserted with textContent and DOM methods only.

const SCHEMA = "po-boys-archive/1";
const DASH = "—";

const state = {
  data: null,
  players: new Map(),
  rulesById: new Map(),
  filters: {},
  navCount: 0,
};

function $(id) { return document.getElementById(id); }

// Small element builder. Children may be strings (inserted as text), nodes, arrays or null.
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  const add = (kid) => {
    if (kid == null || kid === false) return;
    if (Array.isArray(kid)) kid.forEach(add);
    else el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  };
  kids.forEach(add);
  return el;
}

function norm(s) {
  return String(s == null ? "" : s).normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
}

function seasonYear(s) { return parseInt(String(s).slice(0, 4), 10); }
function cmpSeason(a, b) { return seasonYear(a) - seasonYear(b); }

function setStatus(message, isError) {
  const status = $("status");
  status.textContent = message || "";
  status.classList.toggle("error", !!isError);
  status.setAttribute("role", isError ? "alert" : "status");
  status.hidden = !message;
}

// ---------- Links ----------

function scrollToHash(hash) {
  let id;
  try { id = decodeURIComponent(hash.replace(/^#/, "")); } catch (e) { return; }
  const target = $(id.startsWith("rules/") ? "rules-" + id.slice(6) : id);
  if (target) target.scrollIntoView({ block: "start" });
}

// In-page link; a click on the already-current hash still scrolls.
function anchor(href, children, className) {
  const a = h("a", { href, class: className }, children);
  a.addEventListener("click", () => {
    if (location.hash === href) scrollToHash(href);
  });
  return a;
}

// The player's proper name (from the vault); spreadsheet spellings are only fallbacks
// and appear on the player page as "Also recorded as".
function displayName(playerId, fallback) {
  const p = state.players.get(playerId);
  return (p && p.name) || fallback || DASH;
}

function playerLink(playerId, label) {
  if (playerId != null && state.players.has(playerId)) {
    return h("a", { href: "#player/" + encodeURIComponent(playerId) }, label || state.players.get(playerId).name);
  }
  return document.createTextNode(label || DASH);
}

// ---------- Generic filtered, sortable list view ----------

function sortRows(rows, col, dir) {
  const mult = dir === "desc" ? -1 : 1;
  const isMissing = (v) => v == null || v === "" || (typeof v === "number" && Number.isNaN(v));
  return rows
    .map((row, i) => ({ row, i, v: col.value(row) }))
    .sort((a, b) => {
      const am = isMissing(a.v);
      const bm = isMissing(b.v);
      if (am && bm) return a.i - b.i;
      if (am) return 1;
      if (bm) return -1;
      const c = col.type === "text" ? String(a.v).localeCompare(String(b.v)) : a.v - b.v;
      return c ? c * mult : a.i - b.i;
    })
    .map((x) => x.row);
}

function listView(cfg) {
  const rows = cfg.rows;
  const seasons = [...new Set(rows.map((r) => r.season))].sort(cmpSeason);
  const teams = [...new Set(rows.map((r) => r.team).filter(Boolean))].sort((a, b) => a.localeCompare(b));
  const defaults = () => ({
    season: seasons.length ? seasons[seasons.length - 1] : "all",
    team: "",
    q: "",
    sort: { key: cfg.defaultSort, dir: "asc" },
  });
  if (!state.filters[cfg.id]) state.filters[cfg.id] = defaults();
  const st = state.filters[cfg.id];

  const pre = cfg.id + "-";
  const seasonSel = h("select", { id: pre + "season" },
    h("option", { value: "all" }, "All seasons"),
    seasons.slice().reverse().map((s) => h("option", { value: s }, s)));
  const teamSel = h("select", { id: pre + "team" },
    h("option", { value: "" }, "All teams"),
    teams.map((t) => h("option", { value: t }, t)));
  const search = h("input", { id: pre + "q", type: "search", autocomplete: "off", placeholder: "Player name" });
  const reset = h("button", { type: "button", class: "btn" }, "Reset");
  const count = h("p", { class: "count", "aria-live": "polite" });
  const tableHost = h("div", { class: "table-wrap" });
  const emptyMsg = h("p", { class: "note", hidden: true }, cfg.empty);

  const form = h("form", { class: "filters", role: "search", "aria-label": cfg.title + " filters" },
    h("div", { class: "field" }, h("label", { for: pre + "season" }, "Season"), seasonSel),
    h("div", { class: "field" }, h("label", { for: pre + "team" }, "Team"), teamSel),
    h("div", { class: "field field-grow" }, h("label", { for: pre + "q" }, "Player search"), search),
    h("div", { class: "field field-btn" }, reset));
  form.addEventListener("submit", (e) => e.preventDefault());

  function syncControls() {
    seasonSel.value = st.season;
    teamSel.value = st.team;
    search.value = st.q;
  }

  function matches(row) {
    if (st.season !== "all" && row.season !== st.season) return false;
    if (st.team && row.team !== st.team) return false;
    if (st.q) {
      const p = state.players.get(row.player_id);
      const hay = [row.source_name, p && p.name].concat((p && p.aliases) || []).map(norm);
      const needle = norm(st.q.trim());
      if (!hay.some((s) => s.includes(needle))) return false;
    }
    return true;
  }

  function update(focusKey) {
    const cols = cfg.columns(st.season);
    let sortCol = cols.find((c) => c.key === st.sort.key);
    if (!sortCol) { sortCol = cols.find((c) => c.key === cfg.defaultSort); st.sort = { key: cfg.defaultSort, dir: "asc" }; }
    const shown = sortRows(rows.filter(matches), sortCol, st.sort.dir);
    count.textContent = `${shown.length} ${shown.length === 1 ? cfg.noun : cfg.noun + "s"}`;

    const headRow = h("tr");
    for (const col of cols) {
      const active = col.key === st.sort.key;
      const btn = h("button", { type: "button", class: "sort", "data-key": col.key },
        col.label, h("span", { class: "arrow", "aria-hidden": "true" }, active ? (st.sort.dir === "asc" ? " ▲" : " ▼") : ""));
      btn.addEventListener("click", () => {
        if (st.sort.key === col.key) st.sort.dir = st.sort.dir === "asc" ? "desc" : "asc";
        else st.sort = { key: col.key, dir: "asc" };
        update(col.key);
      });
      headRow.append(h("th", {
        scope: "col",
        class: col.cls,
        "aria-sort": active ? (st.sort.dir === "asc" ? "ascending" : "descending") : "none",
      }, btn));
    }
    const body = h("tbody");
    for (const row of shown) {
      const tr = h("tr");
      for (const col of cols) {
        const td = h("td", { class: col.cls });
        col.render(row, td);
        tr.append(td);
      }
      body.append(tr);
    }
    const table = h("table", null, h("caption", { class: "sr-only" }, cfg.title), h("thead", null, headRow), body);
    tableHost.replaceChildren(table);
    tableHost.hidden = shown.length === 0;
    emptyMsg.hidden = shown.length !== 0;
    if (focusKey) {
      const b = tableHost.querySelector(`button[data-key="${focusKey}"]`);
      if (b) b.focus();
    }
  }

  seasonSel.addEventListener("change", () => { st.season = seasonSel.value; update(); });
  teamSel.addEventListener("change", () => { st.team = teamSel.value; update(); });
  search.addEventListener("input", () => { st.q = search.value; update(); });
  reset.addEventListener("click", () => {
    const d = defaults();
    Object.assign(st, d);
    syncControls();
    update();
  });

  syncControls();
  update();
  return h("section", { class: "panel", "aria-labelledby": "view-title" },
    h("h2", { id: "view-title", tabindex: "-1" }, cfg.title), form, count, tableHost, emptyMsg);
}

// ---------- Home ----------

function latestSnapshot() {
  return state.data.rosters.slice().sort(compareSnapshots).pop() || null;
}

function homeView() {
  const d = state.data;
  const view = h("section", { class: "panel home", "aria-labelledby": "view-title" },
    h("h2", { id: "view-title", tabindex: "-1", class: "sr-only" }, "Home"));
  const snap = latestSnapshot();
  const newest = (list) => (list && list.length ? list[list.length - 1] : null);
  const draftSeason = newest(d.seasons.draft);
  const keeperSeason = newest(d.seasons.keeper);

  // Stat tiles: league facts from the vault note, then what's next from the latest roster snapshot.
  const tiles = h("div", { class: "stat-tiles" });
  for (const f of (d.league && d.league.facts) || []) {
    tiles.append(h("div", { class: "stat-tile" }, h("span", { class: "stat-label" }, f.label), h("span", { class: "stat-value" }, f.value)));
  }
  if (snap) {
    tiles.append(h("div", { class: "stat-tile" }, h("span", { class: "stat-label" }, "Up next"),
      h("span", { class: "stat-value" }, [snap.season, snap.label === "Pre-draft" ? "draft" : snap.label].filter(Boolean).join(" "))));
  }
  view.append(h("div", { class: "home-hero" },
    h("p", { class: "home-intro" }, "The league archive: every draft pick, keeper price, roster and rule in one place."),
    tiles));

  // Section cards with live counts.
  const card = (href, title, detail) => h("a", { href, class: "link-card" }, h("span", { class: "link-card-title" }, title),
    h("span", { class: "link-card-detail" }, detail));
  const teamCount = snap ? new Set(snap.entries.map((e) => e.team)).size : 0;
  const ruleDoc = d.rules.find((r) => r.status === "current");
  const ruleCount = ruleDoc ? topItemNumbers(ruleDoc).length : 0;
  const cards = h("nav", { class: "link-cards", "aria-label": "Archive sections" },
    card("#draft", "Draft History", draftSeason ? `${d.drafts.filter((x) => x.season === draftSeason).length} picks · ${draftSeason}` : "No drafts imported"),
    card("#keepers", "Keeper History", keeperSeason ? `${d.keepers.filter((x) => x.season === keeperSeason).length} keepers · ${keeperSeason}` : "No keepers imported"),
    card("#rosters", "Rosters", snap ? `${teamCount} teams · ${snap.entries.length} players · ${snapLabel(snap)}` : "Not imported yet"),
    card("#rules", "Rules", ruleCount ? `${ruleCount} rules` : "League rules"),
    d.suggestions && d.suggestions.form_url ? card("#rules/suggestions", "Suggest a rule change", d.suggestions.season ? "For " + d.suggestions.season : "Send an idea") : null);
  view.append(cards);

  // Highlight: priciest keeper prices on current rosters.
  if (snap && snap.price_from_draft) {
    const top = snap.entries.filter((e) => e.keeper_price != null).sort((a, b) => b.keeper_price - a.keeper_price).slice(0, 5);
    if (top.length) {
      const season = nextSeason(snap.price_from_draft);
      view.append(h("h3", { class: "home-subhead" }, `Priciest keepers${season ? " for " + season : ""}`),
        h("ol", { class: "top-list" }, top.map((e) => h("li", null,
          playerLink(e.player_id, displayName(e.player_id, e.source_name)), h("span", { class: "top-team" }, e.team),
          h("span", { class: "top-price" }, "$" + e.keeper_price)))));
    }
  }
  return view;
}

// ---------- Draft History ----------

// Money as recorded: a number, a text code such as CBK (with its meaning), or missing.
function moneyText(amount, display) {
  if (amount != null) return "$" + amount;
  return display || DASH;
}

function moneyCell(amount, display, note) {
  return (row, td) => {
    const a = amount(row), d = display(row), n = note(row);
    if (a == null && d && n) {
      td.append(h("abbr", { title: n }, d), h("span", { class: "sr-only" }, ` (${n})`));
    } else {
      td.textContent = moneyText(a, d);
    }
  };
}

// Colour-coded fee badge; colours match the league spreadsheet (see style.css .fee-*).
function feeBadge(key, label) {
  if (!label) return null;
  return h("span", { class: "fee" + (key ? " fee-" + key : "") }, label);
}

function nextSeason(season) {
  const ft = (state.data.fee_types || {})[season];
  return ft && ft.applies_to ? ft.applies_to : null;
}

function nbaText(row) {
  const pos = (row.positions || []).join("/");
  return [row.nba_team, pos].filter(Boolean).join(" · ");
}

function textOrDash(td, v) { td.textContent = v == null || v === "" ? DASH : v; }

function draftView() {
  const feeTypes = state.data.fee_types || {};
  return listView({
    id: "draft",
    title: "Draft History",
    noun: "pick",
    empty: "No draft picks match these filters.",
    rows: state.data.drafts,
    defaultSort: "pick",
    columns(season) {
      const ft = feeTypes[season];
      const next = season !== "all" && ft && ft.applies_to ? ft.applies_to : null;
      const feeLabel = next ? `${next} fee` : "Next-season fee";
      const priceLabel = next ? `Keeper price (${next})` : "Next-season keeper price";
      return [
        { key: "season", label: "Season", type: "num", value: (r) => seasonYear(r.season), render: (r, td) => textOrDash(td, r.season) },
        { key: "pick", label: "Pick", type: "num", cls: "num", value: (r) => r.pick, render: (r, td) => textOrDash(td, r.pick) },
        { key: "player", label: "Player", type: "text", cls: "left", value: (r) => displayName(r.player_id, r.source_name), render: (r, td) => td.append(playerLink(r.player_id, displayName(r.player_id, r.source_name))) },
        { key: "team", label: "Team", type: "text", cls: "left", value: (r) => r.team, render: (r, td) => textOrDash(td, r.team) },
        { key: "salary", label: "Draft price", type: "num", cls: "num", value: (r) => r.salary,
          render: moneyCell((r) => r.salary, (r) => r.salary_display, (r) => r.salary_note) },
        { key: "next", label: priceLabel, type: "num", cls: "num", value: (r) => r.next_price,
          render: moneyCell((r) => r.next_price, (r) => r.next_price_display, (r) => r.next_price_note) },
        { key: "nba", label: "NBA team/pos (workbook)", type: "text", cls: "left col-nba", value: nbaText, render: (r, td) => textOrDash(td, nbaText(r)) },
        { key: "fee", label: feeLabel, type: "text", cls: "left col-fee", value: (r) => r.fee_type,
          render: (r, td) => { if (r.fee_type) td.append(feeBadge(r.fee_key, r.fee_type)); else td.textContent = DASH; } },
        { key: "kept", label: "Kept", type: "num", cls: "left", value: (r) => (r.kept ? 1 : 0), render: (r, td) => { if (r.kept) td.append(h("span", { class: "badge badge-kept" }, "Kept")); } },
      ];
    },
  });
}

// ---------- Keeper History ----------

function keeperView() {
  return listView({
    id: "keepers",
    title: "Keeper History",
    noun: "keeper",
    empty: "No keepers match these filters.",
    rows: state.data.keepers,
    defaultSort: "player",
    columns(season) {
      const next = season !== "all" ? nextSeason(season) : null;
      const nextLabel = next ? `Price to keep (${next})` : "Next-season price to keep";
      return [
        { key: "season", label: "Season", type: "num", value: (r) => seasonYear(r.season), render: (r, td) => textOrDash(td, r.season) },
        { key: "player", label: "Player", type: "text", cls: "left", value: (r) => displayName(r.player_id, r.source_name), render: (r, td) => td.append(playerLink(r.player_id, displayName(r.player_id, r.source_name))) },
        { key: "team", label: "Team", type: "text", cls: "left", value: (r) => r.team, render: (r, td) => textOrDash(td, r.team) },
        { key: "cost", label: "Kept at", type: "num", cls: "num", value: (r) => r.cost,
          render: moneyCell((r) => r.cost, (r) => r.cost_display, (r) => r.cost_note) },
        { key: "next", label: nextLabel, type: "num", cls: "num", value: (r) => r.next_cost,
          render: moneyCell((r) => r.next_cost, (r) => r.next_cost_display, (r) => r.next_cost_note) },
        { key: "pick", label: "Draft pick", type: "num", cls: "num", value: (r) => r.draft_pick, render: (r, td) => textOrDash(td, r.draft_pick) },
      ];
    },
  });
}

// ---------- Rules ----------

function renderInline(nodes, parent) {
  for (const n of nodes || []) {
    switch (n.t) {
      case "text": parent.append(document.createTextNode(n.v == null ? "" : n.v)); break;
      case "strong": case "em": case "del": {
        const el = document.createElement(n.t);
        renderInline(n.c, el);
        parent.append(el);
        break;
      }
      case "link": {
        if (n.section) {
          const a = anchor("#rules/" + encodeURIComponent(n.section), null);
          renderInline(n.c, a);
          parent.append(a);
        } else if (state.rulesById.has(n.doc)) {
          const a = anchor("#rules/" + encodeURIComponent(n.doc), null);
          renderInline(n.c, a);
          parent.append(a);
        } else {
          const s = h("span");
          renderInline(n.c, s);
          parent.append(s);
        }
        break;
      }
      case "missing":
        parent.append(h("span", { class: "missing" }, n.v, " ", h("small", null, `(unavailable: ${n.note || "not on this site"})`)));
        break;
      default: break;
    }
  }
}

function renderBlocks(blocks, parent, docId, top, usedIds) {
  for (const b of blocks || []) {
    if (b.t === "p") {
      const p = h("p");
      renderInline(b.c, p);
      parent.append(p);
    } else if (b.t === "heading") {
      const level = Math.min(6, Math.max(4, (b.level || 1) + 3));
      const hd = h("h" + level);
      renderInline(b.c, hd);
      parent.append(hd);
    } else if (b.t === "hr") {
      parent.append(h("hr"));
    } else if (b.t === "list") {
      const list = h(b.ordered ? "ol" : "ul");
      for (const item of b.items || []) {
        const li = h("li");
        if (b.ordered && item.n != null) {
          li.setAttribute("value", String(item.n));
          const id = `rule-${docId}-${item.n}`;
          if (top && !usedIds.has(id)) { li.id = id; usedIds.add(id); }
        }
        renderInline(item.c, li);
        renderBlocks(item.blocks, li, docId, false, usedIds);
        list.append(li);
      }
      parent.append(list);
    }
  }
}

function topItemNumbers(doc) {
  const out = [];
  for (const b of doc.blocks || []) {
    if (b.t !== "list" || !b.ordered) continue;
    for (const item of b.items || []) if (item.n != null && !out.includes(item.n)) out.push(item.n);
  }
  return out;
}

const STATUS_LABELS = { current: "Current", amendments: "Amendments", historical: "Historical" };

function statusBadge(status) {
  return h("span", { class: "badge badge-" + status }, STATUS_LABELS[status] || status);
}

// Suggestions go to the commissioner's Google Form; the site only links to it.
function suggestionsSection(sug) {
  const sec = h("article", { class: "rules-doc suggestions", id: "rules-suggestions", "aria-labelledby": "rules-h-suggestions" },
    h("div", { class: "doc-head" }, h("h3", { id: "rules-h-suggestions" }, sug.title),
      sug.season ? h("span", { class: "badge badge-amendments" }, "For " + sug.season) : null));
  if (sug.intro) sec.append(h("p", null, sug.intro));
  if (sug.form_url) {
    sec.append(h("p", null, h("a", { href: sug.form_url, class: "btn btn-primary", target: "_blank", rel: "noopener noreferrer" },
      "Open the suggestion form"), h("span", { class: "note" }, " Opens Google Forms in a new tab.")));
  } else {
    sec.append(h("p", { class: "note" }, "The suggestion form isn't open yet. Check back soon."));
  }
  return sec;
}

function rulesView() {
  const docs = state.data.rules;
  const view = h("section", { class: "panel", "aria-labelledby": "view-title" },
    h("h2", { id: "view-title", tabindex: "-1" }, "Rules"));
  if (!docs.length) {
    view.append(h("p", { class: "note" }, "No rules documents have been imported."));
    return view;
  }
  const toc = h("nav", { class: "toc", "aria-label": "Rules contents" }, h("h3", null, "Contents"));
  const tocList = h("ul");
  for (const doc of docs) {
    const li = h("li", null, anchor("#rules/" + encodeURIComponent(doc.id), doc.title), " ", statusBadge(doc.status));
    const nums = topItemNumbers(doc);
    if (nums.length) {
      const items = h("span", { class: "toc-items" }, "Items: ");
      nums.forEach((n, i) => {
        if (i) items.append(" ");
        items.append(anchor(`#rule-${encodeURIComponent(doc.id)}-${n}`, String(n), "chip"));
      });
      li.append(h("div", null, items));
    }
    tocList.append(li);
  }
  const sug = state.data.suggestions;
  if (sug) tocList.append(h("li", null, anchor("#rules/suggestions", sug.title)));
  toc.append(tocList);
  view.append(toc);

  for (const doc of docs) {
    const sec = h("article", { class: "rules-doc doc-" + doc.status, id: "rules-" + doc.id, "aria-labelledby": "rules-h-" + doc.id },
      h("div", { class: "doc-head" }, h("h3", { id: "rules-h-" + doc.id }, doc.title), statusBadge(doc.status)),
      h("p", { class: "source" }, "Source: " + doc.source));
    if (doc.status === "historical") {
      sec.append(h("p", { class: "note historical-note" }, "Historical — superseded by the current rules"));
    }
    renderBlocks(doc.blocks, sec, doc.id, true, new Set());
    view.append(sec);
  }
  if (sug) view.append(suggestionsSection(sug));
  return view;
}

// ---------- Rosters ----------

function compareSnapshots(a, b) {
  return cmpSeason(a.season, b.season) || String(a.snapshot_date).localeCompare(String(b.snapshot_date));
}

function formatDate(iso) {
  const d = new Date(/^\d{4}-\d{2}-\d{2}$/.test(iso) ? iso + "T00:00:00" : iso);
  return isNaN(d.getTime()) ? String(iso) : d.toLocaleDateString(undefined, { dateStyle: "medium" });
}

function snapLabel(s) {
  return [s.season, s.label, "Snapshot " + formatDate(s.snapshot_date)].filter(Boolean).join(" · ");
}

function rostersView() {
  const view = h("section", { class: "panel", "aria-labelledby": "view-title" },
    h("h2", { id: "view-title", tabindex: "-1" }, "Rosters"));
  const snaps = state.data.rosters.slice().sort(compareSnapshots).reverse();
  if (!snaps.length) {
    view.append(h("p", { class: "note" }, "Active roster data has not been imported."));
    return view;
  }
  if (!state.filters.rosters || !snaps.some((s) => s.id === state.filters.rosters.snap)) {
    state.filters.rosters = { snap: snaps[0].id };
  }
  const st = state.filters.rosters;
  const sel = h("select", { id: "roster-snap" },
    snaps.map((s) => h("option", { value: s.id }, snapLabel(s))));
  sel.value = st.snap;
  const body = h("div");
  function draw() {
    const snap = snaps.find((s) => s.id === st.snap);
    body.replaceChildren();
    body.append(h("p", { class: "count" }, snapLabel(snap)));
    const priceSeason = snap.price_from_draft ? nextSeason(snap.price_from_draft) : null;
    if (snap.price_from_draft) {
      body.append(h("p", { class: "note" },
        `Keeper prices${priceSeason ? " for " + priceSeason : ""} are as recorded in the ${snap.price_from_draft} draft sheet.`,
        snap.undrafted_rule
          ? ` Players not drafted in ${snap.price_from_draft} are shown at ${moneyText(snap.undrafted_price, null)}: ${snap.undrafted_rule}`
          : ""));
    }
    const byTeam = new Map();
    for (const e of snap.entries || []) {
      if (!byTeam.has(e.team)) byTeam.set(e.team, []);
      byTeam.get(e.team).push(e);
    }
    const priced = !!snap.price_from_draft;
    const price = moneyCell((e) => e.keeper_price, (e) => e.keeper_price_display, (e) => e.keeper_price_note);
    const grid = h("div", { class: "cards" });
    for (const team of [...byTeam.keys()].sort((a, b) => String(a).localeCompare(String(b)))) {
      const entries = byTeam.get(team);
      const tbody = h("tbody");
      entries.sort((a, b) => displayName(a.player_id, a.source_name).localeCompare(displayName(b.player_id, b.source_name)));
      for (const e of entries) {
        const tr = h("tr", null, h("td", { class: "left" }, playerLink(e.player_id, displayName(e.player_id, e.source_name))));
        if (priced) {
          const td = h("td", { class: "num" });
          price(e, td);
          const moved = e.price_basis === "drafted" && e.drafted_by && e.drafted_by !== team;
          tr.append(td, h("td", { class: "left roster-note" },
            feeBadge(e.fee_key, e.fee_short || (e.price_basis === "undrafted" ? "Undrafted" : null)),
            moved ? h("span", { class: "moved" }, "Drafted by " + e.drafted_by) : null));
        }
        tbody.append(tr);
      }
      const head = h("tr", null, h("th", { scope: "col", class: "left" }, "Player"),
        priced ? [h("th", { scope: "col", class: "num", title: priceSeason ? `Keeper price for ${priceSeason}` : "Keeper price" }, "Price"),
                  h("th", { scope: "col", class: "left" }, "Note")] : null);
      grid.append(h("div", { class: "card" },
        h("h3", null, team, h("span", { class: "card-count" }, ` · ${entries.length} players`)),
        h("div", { class: "table-wrap" }, h("table", null, h("caption", { class: "sr-only" }, team + " roster"),
          h("thead", null, head), tbody))));
    }
    body.append(grid);
  }
  sel.addEventListener("change", () => { st.snap = sel.value; draw(); });
  if (snaps.length > 1) {
    const form = h("form", { class: "filters" }, h("div", { class: "field" }, h("label", { for: "roster-snap" }, "Snapshot"), sel));
    form.addEventListener("submit", (e) => e.preventDefault());
    view.append(form);
  }
  view.append(body);
  draw();
  return view;
}

// ---------- Player detail ----------

function simpleTable(caption, headers, rows) {
  const tbody = h("tbody");
  for (const r of rows) {
    tbody.append(h("tr", null, r.map((c, i) => h("td", { class: headers[i].cls }, c == null || c === "" ? DASH : c))));
  }
  return h("div", { class: "table-wrap" }, h("table", null,
    h("caption", { class: "sr-only" }, caption),
    h("thead", null, h("tr", null, headers.map((x) => h("th", { scope: "col", class: x.cls }, x.label)))),
    tbody));
}

function backLink() {
  const a = h("a", { href: "#home", class: "back" }, "← Back");
  a.addEventListener("click", (e) => {
    // Only step back within this page's own navigation, never off the site.
    if (state.navCount > 0) { e.preventDefault(); history.back(); }
  });
  return a;
}

function playerView(id) {
  const p = state.players.get(id);
  const view = h("section", { class: "panel", "aria-labelledby": "view-title" }, backLink());
  if (!p) {
    view.append(h("h2", { id: "view-title", tabindex: "-1" }, "Player"), h("p", { class: "note" }, "Player not found."));
    return view;
  }
  view.append(h("h2", { id: "view-title", tabindex: "-1" }, p.name));
  if (p.aliases && p.aliases.length) view.append(h("p", { class: "note" }, "Also recorded as " + p.aliases.join(", ")));

  const vault = h("div", { class: "vault" }, h("h3", null, "From the league vault"));
  if (p.in_vault) {
    vault.append(h("dl", null,
      h("dt", null, "NBA team"), h("dd", null, p.nba_team || DASH),
      h("dt", null, "Positions"), h("dd", null, (p.positions || []).join(", ") || DASH)));
  } else {
    vault.append(h("p", { class: "note" }, "No vault note for this player"));
  }
  view.append(vault);

  const none = () => h("p", { class: "note" }, "None recorded");
  const drafts = state.data.drafts.filter((d) => d.player_id === id);
  view.append(h("h3", null, "Draft records"));
  view.append(drafts.length ? simpleTable("Draft records", [
    { label: "Season" }, { label: "Pick", cls: "num" }, { label: "Team" }, { label: "Draft price", cls: "num" },
    { label: "Next-season keeper price", cls: "num" }, { label: "Next-season fee" }, { label: "Kept" },
  ], drafts.map((d) => [d.season, d.pick, d.team, moneyText(d.salary, d.salary_display),
    moneyText(d.next_price, d.next_price_display) + (nextSeason(d.season) ? ` (${nextSeason(d.season)})` : ""),
    feeBadge(d.fee_key, d.fee_type), d.kept ? "Kept" : ""])) : none());

  const keepers = state.data.keepers.filter((k) => k.player_id === id);
  view.append(h("h3", null, "Keeper records"));
  view.append(keepers.length ? simpleTable("Keeper records", [
    { label: "Season" }, { label: "Team" }, { label: "Kept at", cls: "num" }, { label: "Next-season price to keep", cls: "num" },
    { label: "Draft pick", cls: "num" },
  ], keepers.map((k) => [k.season, k.team, moneyText(k.cost, k.cost_display),
    moneyText(k.next_cost, k.next_cost_display) + (nextSeason(k.season) ? ` (${nextSeason(k.season)})` : ""),
    k.draft_pick])) : none());

  const memberships = [];
  for (const s of state.data.rosters.slice().sort(compareSnapshots)) {
    for (const e of s.entries || []) {
      if (e.player_id !== id) continue;
      const basis = h("span", null, feeBadge(e.fee_key, e.fee_short),
        e.drafted_by ? ` Drafted by ${e.drafted_by}, pick ${e.draft_pick}` : "");
      memberships.push([[s.season, s.label].filter(Boolean).join(" "), formatDate(s.snapshot_date), e.team,
        e.price_basis ? moneyText(e.keeper_price, e.keeper_price_display) : "", basis]);
    }
  }
  view.append(h("h3", null, "Roster memberships"));
  view.append(memberships.length ? simpleTable("Roster memberships", [
    { label: "Season" }, { label: "Snapshot" }, { label: "Team" }, { label: "Keeper price", cls: "num" }, { label: "Price basis" },
  ], memberships) : none());
  return view;
}

// ---------- Router ----------

function parseRoute() {
  let raw = location.hash.replace(/^#/, "");
  try { raw = decodeURIComponent(raw); } catch (e) { /* keep raw */ }
  if (raw.startsWith("rule-")) return { view: "rules", scroll: raw };
  const [name, ...rest] = raw.split("/");
  const arg = rest.join("/");
  switch (name) {
    case "": case "home": return { view: "home" };
    case "draft": return { view: "draft" };
    case "keepers": return { view: "keepers" };
    case "rules": return { view: "rules", scroll: arg ? "rules-" + arg : null };
    case "rosters": return { view: "rosters" };
    case "player": return { view: "player", id: arg };
    default: return { view: "home" };
  }
}

let renderedKey = null;

function route() {
  if (!state.data) return;
  const r = parseRoute();
  const key = r.view + ":" + (r.id || "");
  const same = key === renderedKey;
  if (!same) {
    const builders = { home: homeView, draft: draftView, keepers: keeperView, rules: rulesView, rosters: rostersView, player: () => playerView(r.id) };
    const view = builders[r.view]();
    $("view").replaceChildren(view);
    const labels = { home: "Home", draft: "Draft History", keepers: "Keeper History", rules: "Rules", rosters: "Rosters", player: "Player" };
    const site = (state.data.league && state.data.league.name) || "League archive";
    document.title = r.view === "home" ? site : `${labels[r.view]} · ${site}`;
    for (const a of document.querySelectorAll("#nav a")) {
      if (a.dataset.view === r.view) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    }
    renderedKey = key;
  }
  if (r.scroll) {
    const t = $(r.scroll);
    if (t) t.scrollIntoView({ block: "start" });
  } else if (!same) {
    window.scrollTo(0, 0);
    if (renderedKey !== null && route.started) {
      const t = $("view-title");
      if (t) t.focus({ preventScroll: true });
    }
  }
  route.started = true;
}

// ---------- Loading ----------

function renderHeader() {
  const d = state.data;
  if (d.league) {
    if (d.league.name) $("league-name").textContent = d.league.name;
    $("league-short").textContent = d.league.short_name || "";
    $("league-short").hidden = !d.league.short_name;
  }
  const all = new Set();
  for (const k of ["draft", "keeper", "roster"]) for (const s of (d.seasons && d.seasons[k]) || []) all.add(s);
  const list = [...all].sort(cmpSeason);
  $("league-seasons").textContent = list.length ? "Seasons: " + list.join(", ") : "";
  const when = new Date(d.imported_at);
  $("updated").textContent = isNaN(when.getTime()) ? "" : "Archive imported " + when.toLocaleDateString(undefined, { dateStyle: "medium" });
}

function validate(d) {
  if (!d || typeof d !== "object") return "the data is not an object";
  if (d.schema !== SCHEMA) return "unrecognised schema version";
  for (const k of ["drafts", "keepers", "rosters", "rules", "players"]) {
    if (!Array.isArray(d[k])) return `missing "${k}" list`;
  }
  return null;
}

async function load() {
  let response;
  try {
    response = await fetch("archive.json", { cache: "no-store" });
  } catch (err) {
    setStatus("Could not reach the archive. Check your connection and reload.", true);
    return;
  }
  if (!response.ok) {
    setStatus(response.status === 404
      ? "The archive has not been published yet."
      : `The archive could not be loaded (HTTP ${response.status}).`, true);
    return;
  }
  let data;
  try {
    data = await response.json();
  } catch (err) {
    setStatus("The archive file is damaged (not valid JSON).", true);
    return;
  }
  const problem = validate(data);
  if (problem) {
    setStatus("The archive is in an unexpected format: " + problem + ".", true);
    return;
  }
  state.data = data;
  for (const p of data.players) state.players.set(p.id, p);
  for (const r of data.rules) state.rulesById.set(r.id, r);
  renderHeader();
  setStatus("");
  route();
}

window.addEventListener("hashchange", () => { state.navCount += 1; route(); });
load();
