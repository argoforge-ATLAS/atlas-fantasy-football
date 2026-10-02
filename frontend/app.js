const content = document.getElementById("content");
const tabsEl = document.getElementById("leagueTabs");
const refreshBtn = document.getElementById("refreshBtn");

let leagues = [];
let activeLeagueId = null;

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} -> ${res.status}`);
  return res.json();
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

async function loadLeagueView(leagueId) {
  content.innerHTML = '<p class="loading">Loading...</p>';
  loadRecommendations(leagueId);
  try {
    const [matchup, lineup] = await Promise.all([
      fetchJSON(`/api/leagues/${leagueId}/matchup`),
      fetchJSON(`/api/leagues/${leagueId}/lineup`),
    ]);

    let html = "";

    html += `<div class="card"><h2>This Week - Week ${matchup.week || ""}</h2>`;
    if (matchup.message) {
      html += `<p>${matchup.message}</p>`;
    } else {
      html += `
        <p class="hint">Live scores - both sides show 0 until games for the week are played.</p>
        <div class="matchup-row">
          <span>You (${matchup.me.team_name})</span>
          <span class="score">${matchup.me.points} pts</span>
        </div>
        <div class="matchup-row">
          <span>Opponent (${matchup.opponent.team_name})</span>
          <span class="score">${matchup.opponent.points} pts</span>
        </div>`;
    }
    html += "</div>";

    html += '<div class="card"><h2>Starters</h2><div class="player-list">';
    html += lineup.starters.map(p => `<div class="player-row"><span>${p.name}</span><span class="pos">${p.position || ""}${p.team ? " · " + p.team : ""}</span></div>`).join("");
    html += "</div></div>";

    html += '<div class="card"><h2>Bench</h2><div class="player-list">';
    html += lineup.bench.map(p => `<div class="player-row"><span>${p.name}</span><span class="pos">${p.position || ""}${p.team ? " · " + p.team : ""}</span></div>`).join("");
    html += "</div></div>";

    content.innerHTML = html;
  } catch (err) {
    content.innerHTML = `<p class="error">Couldn't load this league: ${err.message}</p>`;
  }
}

refreshBtn.onclick = () => activeLeagueId && loadLeagueView(activeLeagueId);

const adviceOutput = document.getElementById("adviceOutput");

async function loadRecommendations(leagueId) {
  adviceOutput.innerHTML = '<p class="loading">Loading...</p>';
  try {
    const data = await fetchJSON(`/api/leagues/${leagueId}/recommendations`);
    let html = "";

    if (data.swap_suggestions.length === 0) {
      html += "<p>No swaps suggested right now - your lineup looks set.</p>";
    } else {
      html += '<div class="player-list">';
      html += data.swap_suggestions.map(s => `
        <div class="player-row">
          <span><strong>Start ${s.start}</strong></span>
          <span class="pos">over ${s.sit} (${s.slot})</span>
          <span class="hint">${s.reason}</span>
        </div>`).join("");
      html += "</div>";
    }

    html += '<h3 style="margin-top:1.25rem;">Top waiver targets</h3><div class="player-list">';
    html += data.top_waivers.map(w => {
      const prod = w.recent_avg_points !== null ? `${w.recent_avg_points} pts/gm` : "no recent stats";
      const fc = w.fantasycalc_value !== null ? `FC value ${w.fantasycalc_value}` : "unranked";
      return `<div class="player-row"><span>${w.name}</span><span class="pos">${w.position || "?"} · ${prod} · ${fc}</span></div>`;
    }).join("");
    html += "</div>";

    adviceOutput.innerHTML = html;
  } catch (err) {
    adviceOutput.innerHTML = `<p class="error">Couldn't load recommendations: ${err.message}</p>`;
  }
}

loadLeagues().catch(err => {
  content.innerHTML = `<p class="error">Couldn't load leagues: ${err.message}</p>`;
});
