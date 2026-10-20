const content = document.getElementById("content");
const tabsEl = document.getElementById("leagueTabs");
const refreshBtn = document.getElementById("refreshBtn");
const overlayLink = document.getElementById("overlayLink");

const out = {
  scoreboard: document.getElementById("scoreboardOutput"),
  advice: document.getElementById("adviceOutput"),
  waiver: document.getElementById("waiverOutput"),
  drops: document.getElementById("dropCandidatesOutput"),
  needs: document.getElementById("teamNeedsOutput"),
  tradeTargets: document.getElementById("tradeTargetsOutput"),
  tradeEval: document.getElementById("tradeEvalOutput"),
};

let leagues = [];
let activeLeagueId = null;

// Every load is stamped with a token. A slow response for a league you
// have already switched away from must NOT overwrite the panels, so
// each render checks that its token is still the current one.
let viewToken = 0;
let livePoll = null;

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
}

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} -> ${res.status}`);
  return res.json();
}

// Run one panel's load. Renders only if the user is still on the same
// league/view; otherwise the result is silently dropped.
async function panel(token, el, url, render, label) {
  el.innerHTML = '<p class="loading">Loading...</p>';
  try {
    const data = await fetchJSON(url);
    if (token !== viewToken) return;
    el.innerHTML = render(data);
  } catch (err) {
    if (token !== viewToken) return;
    el.innerHTML = `<p class="error">Couldn't load ${esc(label)}: ${esc(err.message)}</p>`;
  }
}

async function loadLeagues() {
  leagues = await fetchJSON("/api/leagues");
  tabsEl.innerHTML = "";
  leagues.forEach((lg, i) => {
    const btn = document.createElement("button");
    btn.className = "tab" + (i === 0 ? " active" : "");
    btn.textContent = lg.name || lg.label;
    btn.dataset.id = lg.id;
    btn.onclick = () => selectLeague(lg.id);
    tabsEl.appendChild(btn);
  });
  if (leagues.length) {
    activeLeagueId = leagues[0].id;
    await loadLeagueView(activeLeagueId);
  }
}

function setActiveTab(leagueId) {
  [...tabsEl.children].forEach(btn => {
    btn.classList.toggle("active", btn.dataset.id === leagueId);
  });
}

async function selectLeague(leagueId) {
  activeLeagueId = leagueId;
  setActiveTab(leagueId);
  await loadLeagueView(leagueId);
}

// ---------------------------------------------------------------- helpers

const MATCHUP_LABEL_CLASS = {
  "Tough matchup": "label-weakness",
  "Average matchup": "label-average",
  "Good matchup": "label-strength",
};

const MATCHUP_CHIP_CLASS = {
  "Tough matchup": "tough",
  "Average matchup": "average",
  "Good matchup": "good",
};

function injuryBadge(player) {
  if (!player.injury_status) return "";
  const cls = player.bad_injury ? "label-weakness" : "label-average";
  return ` <span class="${cls}">${esc(player.injury_status)}</span>`;
}

function matchupChips(upcoming) {
  if (!upcoming || !upcoming.length) return "";
  const chips = upcoming.map(u => {
    const cls = MATCHUP_CHIP_CLASS[u.matchup_label] || "unknown";
    const text = u.opponent || "BYE";
    return `<span class="matchup-chip ${cls}" title="Week ${esc(u.week)}${u.matchup_label ? ": " + esc(u.matchup_label) : ""}">${esc(text)}</span>`;
  }).join("");
  return `<span class="matchup-chips">${chips}</span>`;
}

const fmt = (n, d = 1) => (n === null || n === undefined ? "–" : Number(n).toFixed(d));

function table(headers, rows, cls = "") {
  if (!rows.length) return "";
  const head = headers.map(h => `<th>${esc(h)}</th>`).join("");
  return `<div class="table-wrap"><table class="data ${cls}"><thead><tr>${head}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
}

// ---------------------------------------------------------------- scoreboard

function sparkline(history) {
  if (!history || history.length < 2) return '<p class="hint">Win-probability trend appears as the week plays out.</p>';
  const w = 300, h = 56, pad = 4;
  const xs = history.map((_, i) => pad + (i / (history.length - 1)) * (w - pad * 2));
  const ys = history.map(pt => pad + (1 - pt.p / 100) * (h - pad * 2));
  const pts = xs.map((x, i) => `${x.toFixed(1)},${ys[i].toFixed(1)}`).join(" ");
  const mid = (pad + (h - pad * 2) / 2).toFixed(1);
  const last = history[history.length - 1].p;
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="Win probability over time, now ${last}%">
    <line x1="0" x2="${w}" y1="${mid}" y2="${mid}" class="spark-mid"/>
    <polyline points="${pts}" class="spark-line ${last >= 50 ? "up" : "down"}"/>
  </svg>`;
}

function statusPill(p) {
  const text = { final: "Done", live: "Live", upcoming: "Later", bye: "Bye" }[p.status] || "";
  return `<span class="pill pill-${esc(p.status)}">${text}</span>`;
}

function playerCell(p) {
  if (!p) return "<td></td><td></td>";
  const exp = p.status === "final" || p.status === "bye" ? "" : ` <span class="hint">proj ${fmt(p.expected)}</span>`;
  return `<td>${esc(p.name)}${injuryBadge(p)} ${statusPill(p)}${exp}</td><td class="num">${fmt(p.points, 2)}</td>`;
}

function renderScoreboard(d) {
  if (d.no_matchup) return "<p>No head-to-head this week (bye or schedule not set yet).</p>";
  const me = d.me, opp = d.opponent;
  const leading = d.margin > 0, tied = d.margin === 0;
  const meCls = tied ? "" : (leading ? "lead" : "trail");
  const oppCls = tied ? "" : (leading ? "trail" : "lead");

  const rows = [];
  const n = Math.max(me.players.length, opp.players.length);
  for (let i = 0; i < n; i++) {
    const a = me.players[i], b = opp.players[i];
    rows.push(`<tr><td class="slot">${esc((a || b).slot || "")}</td>${playerCell(a)}${playerCell(b)}</tr>`);
  }

  return `
    <div class="scoreboard">
      <div class="side ${meCls}">
        <div class="team">${esc(me.team_name)}</div>
        <div class="score">${fmt(me.points, 2)}</div>
        <div class="hint">proj ${fmt(me.projected_final)} · ${me.yet_to_play} to play</div>
      </div>
      <div class="vs">
        <div class="winprob">${fmt(d.win_prob, 0)}%</div>
        <div class="hint">win prob</div>
      </div>
      <div class="side ${oppCls}">
        <div class="team">${esc(opp.team_name)}</div>
        <div class="score">${fmt(opp.points, 2)}</div>
        <div class="hint">proj ${fmt(opp.projected_final)} · ${opp.yet_to_play} to play</div>
      </div>
    </div>
    <div class="probbar"><div class="probfill" style="width:${Math.max(0, Math.min(100, d.win_prob))}%"></div></div>
    ${d.headline ? `<p class="headline">${esc(d.headline)}</p>` : ""}
    ${sparkline(d.history)}
    <div class="table-wrap"><table class="data compare">
      <thead><tr><th></th><th>You</th><th class="num">Pts</th><th>Opponent</th><th class="num">Pts</th></tr></thead>
      <tbody>${rows.join("")}</tbody>
    </table></div>`;
}

function loadScoreboard(token, leagueId) {
  return panel(token, out.scoreboard, `/api/leagues/${leagueId}/live-matchup`, renderScoreboard, "the matchup");
}

// Quietly refresh the scoreboard without the "Loading..." flash.
async function refreshScoreboardQuietly(token, leagueId) {
  try {
    const data = await fetchJSON(`/api/leagues/${leagueId}/live-matchup`);
    if (token !== viewToken) return;
    out.scoreboard.innerHTML = renderScoreboard(data);
  } catch (_) { /* keep what is on screen */ }
}

function startLivePolling(token, leagueId) {
  if (livePoll) clearInterval(livePoll);
  livePoll = setInterval(() => {
    if (document.hidden || token !== viewToken) return;
    refreshScoreboardQuietly(token, leagueId);
  }, 30000);
}

// ---------------------------------------------------------------- recommendations / waivers

function renderAdvice(data) {
  let html = "";
  if (!data.swap_suggestions.length) {
    html += "<p>No swaps suggested right now - your lineup looks set.</p>";
  } else {
    const rows = data.swap_suggestions.map(s => `
      <tr>
        <td class="slot">${esc(s.slot)}</td>
        <td><strong>${s.start ? `Start ${esc(s.start)}` : `Bench ${esc(s.sit)}`}</strong>${s.start ? `<div class="hint">over ${esc(s.sit)}</div>` : ""}</td>
        <td class="num">${s.gain !== null && s.gain !== undefined ? "+" + fmt(s.gain) : "–"}</td>
        <td class="hint">${esc(s.reason)}</td>
      </tr>`);
    html += table(["Slot", "Move", "Gain", "Why"], rows);
  }

  html += '<h3>Best free agents</h3>';
  html += data.top_waivers.length ? table(["Player", "Pos", "Proj", "Next 3"], data.top_waivers.map(w => `
      <tr>
        <td>${esc(w.name)}${injuryBadge(w)}</td>
        <td>${esc(w.position || "?")}${w.age ? ` <span class="hint">${esc(w.age)}</span>` : ""}</td>
        <td class="num">${fmt(w.expected_points)}</td>
        <td>${matchupChips(w.upcoming_matchups)}</td>
      </tr>`)) : "<p>No free agents stand out.</p>";
  return html;
}

function planRows(plan) {
  return plan.map(r => `
    <tr>
      <td class="num"><strong>${esc(r.claim_order)}</strong></td>
      <td><span class="label-strength">ADD</span> <strong>${esc(r.add.name)}</strong>
        <div class="hint">${esc(r.add.position)} · proj ${fmt(r.add.expected_points)}${r.add.injury_status ? " · " + esc(r.add.injury_status) : ""}</div></td>
      <td><span class="label-weakness">DROP</span> <strong>${esc(r.drop.name)}</strong>
        <div class="hint">${esc(r.drop.position)} · proj ${fmt(r.drop.expected_points)}</div></td>
      <td class="num">${r.fills_slot ? "fills slot" : (r.long_term ? "long-term" : "+" + fmt(r.gain))}</td>
    </tr>
    ${r.warning ? `<tr class="warn-row"><td></td><td colspan="3" class="hint">⚠ ${esc(r.warning)}</td></tr>` : ""}`);
}

function renderWaiverPlan(data) {
  const plan = data.waiver_plan || [];
  const fallback = data.fallback_plan || [];
  const open = (data.open_slots || []).length
    ? `<p class="hint">Empty lineup slot this week: <strong>${esc(data.open_slots.join(", "))}</strong>.</p>` : "";
  let html = open;
  if (plan.length) {
    html += table(["#", "Add", "Drop", "Gain"], planRows(plan), "plan");
  } else {
    html += "<p>No safe add/drop moves this week - everyone you could cut is held for long-term value.</p>";
  }
  if (fallback.length) {
    html += "<h3>If you must drop</h3>"
      + "<p class=\"hint\">Last resort. These players are held for market value, so cutting one gives that value away. Young and injured players are never listed here.</p>"
      + table(["#", "Add", "Drop", "Gain"], planRows(fallback), "plan");
  }
  return html;
}

function loadAdvice(token, leagueId) {
  // One request feeds both the recommendations and the waiver plan.
  out.advice.innerHTML = '<p class="loading">Loading...</p>';
  out.waiver.innerHTML = '<p class="loading">Loading...</p>';
  return fetchJSON(`/api/leagues/${leagueId}/recommendations`).then(data => {
    if (token !== viewToken) return;
    out.advice.innerHTML = renderAdvice(data);
    out.waiver.innerHTML = renderWaiverPlan(data);
  }).catch(err => {
    if (token !== viewToken) return;
    const msg = `<p class="error">Couldn't load recommendations: ${esc(err.message)}</p>`;
    out.advice.innerHTML = msg;
    out.waiver.innerHTML = msg;
  });
}

// ---------------------------------------------------------------- drops / needs

function renderDrops(data) {
  if (!data.candidates.length) return "<p>Nothing standing out as droppable right now.</p>";
  let html = "";
  if (data.enforce_keeper_rule) {
    html += '<p class="hint">Keeper-eligible players are flagged - dropping them costs that eligibility for good.</p>';
  }
  const rows = data.candidates.map(c => {
    const hold = c.verdict !== "Drop candidate";
    const keeper = c.keeper_eligible ? ' <span class="label-keeper">Keeper eligible</span>' : "";
    const opp = c.opportunity_pct !== null && c.opportunity_pct !== undefined
      ? `<div class="hint">${esc(c.opportunity_pct)}% share${c.opportunity_trend ? " (" + esc(c.opportunity_trend.toLowerCase()) + ")" : ""}</div>` : "";
    return `
      <tr>
        <td><strong>${esc(c.name)}</strong>${injuryBadge(c)}${keeper}${opp}</td>
        <td>${esc(c.position || "?")}${c.age ? ` <span class="hint">${esc(c.age)}${c.age_outlook ? " · " + esc(c.age_outlook) : ""}</span>` : ""}</td>
        <td class="num">${fmt(c.recent_avg_points)}</td>
        <td class="num">${c.fantasycalc_value !== null && c.fantasycalc_value !== undefined ? esc(c.fantasycalc_value) : "–"}</td>
        <td><span class="${hold ? "label-strength" : "label-weakness"}">${esc(c.verdict)}</span></td>
      </tr>`;
  });
  return html + table(["Player", "Pos/Age", "Recent", "Value", "Verdict"], rows);
}

function ordinal(n) {
  const s = ["th", "st", "nd", "rd"], v = n % 100;
  return n + (s[(v - 20) % 10] || s[v] || s[0]);
}

function renderNeeds(data) {
  if (!data.needs.length) return "<p>Not enough recent stats yet to judge this.</p>";
  const labelClass = { Strength: "label-strength", Weakness: "label-weakness", Average: "label-average" };
  const max = Math.max(...data.needs.flatMap(n => [n.my_avg, n.league_avg]), 1);
  return '<div class="needs">' + data.needs.map(n => {
    const mine = (n.my_avg / max) * 100;
    const lg = (n.league_avg / max) * 100;
    const barCls = n.label === "Strength" ? "bar-good" : n.label === "Weakness" ? "bar-bad" : "bar-mid";
    return `
      <div class="need-row">
        <div class="need-head">
          <strong>${esc(n.position)}</strong>
          <span class="${labelClass[n.label]}">${esc(n.label)}</span>
          <span class="hint">${ordinal(n.rank)} of ${esc(n.out_of)} · ${fmt(n.my_avg)} vs ${fmt(n.league_avg)}</span>
        </div>
        <div class="bar-track">
          <div class="bar ${barCls}" style="width:${mine.toFixed(1)}%"></div>
          <div class="bar-marker" style="left:${lg.toFixed(1)}%" title="League average"></div>
        </div>
      </div>`;
  }).join("") + '<p class="hint">Bar = your starters\' avg pts; tick = rest-of-league average.</p></div>';
}

// ---------------------------------------------------------------- trades

function renderTargets(data) {
  if (!data.targets.length) return "<p>No targets found.</p>";
  const rows = data.targets.map(t => `
    <tr>
      <td><strong>${esc(t.name)}</strong><div class="hint">${esc(t.owner)}</div></td>
      <td>${esc(t.position)}${t.age ? ` <span class="hint">${esc(t.age)}${t.age_outlook ? " · " + esc(t.age_outlook) : ""}</span>` : ""}</td>
      <td class="num">${esc(t.price)}${t.tax > 1 ? ' <span class="label-keeper">tax</span>' : ""}</td>
      <td class="hint">${t.suggested_package ? esc(t.suggested_package.join(" + ")) : "No package from your bench"}</td>
    </tr>`);
  return `<p class="hint">Weak spots: ${esc(data.weak_positions.join(", "))}</p>` +
    table(["Target", "Pos/Age", "Price", "Package from your bench"], rows);
}

function renderTradeEval(d) {
  const side = rows => rows.map(r =>
    `<li>${esc(r.label)} <span class="hint">${r.value === null || r.value === undefined ? "n/a" : esc(Math.round(r.adjusted))}${r.tax > 1 ? " (taxed)" : ""}</span></li>`).join("") || "<li class=\"hint\">nothing</li>";
  const cls = d.verdict === "You win this trade" ? "label-strength" : d.verdict === "You lose this trade" ? "label-weakness" : "label-average";
  const taxed = d.get_price > d.get_total;
  const accept = d.get.length && d.give.length
    ? (d.likely_accepted
        ? '<span class="label-strength">They\'d likely accept</span>'
        : `<span class="label-weakness">They'd likely want ~${esc(d.ask_more)} more</span>`)
    : "";
  return `
    <div class="trade-result">
      <p><span class="${cls}"><strong>${esc(d.verdict)}</strong></span> <span class="hint">${d.edge_pct > 0 ? "+" : ""}${esc(d.edge_pct)}% on market value</span></p>
      ${accept ? `<p>${accept}${taxed ? ` <span class="hint">(their price: ${esc(d.get_price)} incl. team tax)</span>` : ""}</p>` : ""}
      <div class="trade-cols">
        <div><h4>You give <span class="hint">${esc(d.give_total)}</span></h4><ul>${side(d.give)}</ul></div>
        <div><h4>You get <span class="hint">${esc(d.get_total)}</span></h4><ul>${side(d.get)}</ul></div>
      </div>
      ${(d.warnings || []).map(w => `<p class="hint">⚠ ${esc(w)}</p>`).join("")}
    </div>`;
}

document.getElementById("tradeForm").addEventListener("submit", async ev => {
  ev.preventDefault();
  if (!activeLeagueId) return;
  const token = viewToken;
  const give = document.getElementById("tradeGive").value;
  const get = document.getElementById("tradeGet").value;
  out.tradeEval.innerHTML = '<p class="loading">Evaluating...</p>';
  try {
    const data = await fetchJSON(`/api/leagues/${activeLeagueId}/trade-eval?give=${encodeURIComponent(give)}&get=${encodeURIComponent(get)}`);
    if (token !== viewToken) return;
    out.tradeEval.innerHTML = renderTradeEval(data);
  } catch (err) {
    if (token !== viewToken) return;
    out.tradeEval.innerHTML = `<p class="error">Couldn't evaluate: ${esc(err.message)}</p>`;
  }
});

// ---------------------------------------------------------------- NFL matchups

function renderNflMatchups(data) {
  let html = `<div class="card"><h2>Week ${esc(data.week || "")} NFL matchups</h2>`;
  html += '<p class="hint">Your roster\'s real-life opponents this week, and how tough each defense has been against that position recently.</p>';
  const relevant = data.players.filter(p => p.opponent || p.on_bye);
  if (!relevant.length) {
    html += "<p>No matchup data yet - check back once the week's schedule is set.</p></div>";
    return html;
  }
  const rows = relevant.map(p => {
    let label;
    if (p.on_bye) {
      label = '<span class="label-weakness">Bye week</span>';
    } else {
      const cls = MATCHUP_LABEL_CLASS[p.matchup_label] || "label-average";
      label = p.matchup_label ? `<span class="${cls}">${esc(p.matchup_label)}</span>` : '<span class="hint">not enough data yet</span>';
    }
    return `
      <tr>
        <td><strong>${esc(p.name)}</strong>${injuryBadge(p)}</td>
        <td>${esc(p.position)}${p.on_bye ? " · BYE" : " vs " + esc(p.opponent)}</td>
        <td class="num">${fmt(p.expected_points)}</td>
        <td>${label}</td>
      </tr>`;
  });
  html += table(["Player", "Game", "Exp", "Matchup"], rows) + "</div>";
  return html;
}

// ---------------------------------------------------------------- main load

async function loadLeagueView(leagueId) {
  const token = ++viewToken;
  overlayLink.href = `overlay.html?league=${encodeURIComponent(leagueId)}`;
  content.innerHTML = '<p class="loading">Loading...</p>';

  // Everything loads in parallel; each panel renders independently and
  // only if this is still the league on screen.
  loadScoreboard(token, leagueId);
  loadAdvice(token, leagueId);
  panel(token, out.drops, `/api/leagues/${leagueId}/drop-candidates`, renderDrops, "drop candidates");
  panel(token, out.needs, `/api/leagues/${leagueId}/team-needs`, renderNeeds, "team needs");
  panel(token, out.tradeTargets, `/api/leagues/${leagueId}/trade-targets`, renderTargets, "trade targets");
  out.tradeEval.innerHTML = "";
  startLivePolling(token, leagueId);

  try {
    const data = await fetchJSON(`/api/leagues/${leagueId}/matchups`);
    if (token !== viewToken) return;
    content.innerHTML = renderNflMatchups(data);
  } catch (err) {
    if (token !== viewToken) return;
    content.innerHTML = `<p class="error">Couldn't load this league: ${esc(err.message)}</p>`;
  }
}

refreshBtn.onclick = () => activeLeagueId && loadLeagueView(activeLeagueId);

loadLeagues().catch(err => {
  content.innerHTML = `<p class="error">Couldn't load leagues: ${esc(err.message)}</p>`;
});
