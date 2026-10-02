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
  loadTeamNeeds(leagueId);
  try {
    const matchup = await fetchJSON(`/api/leagues/${leagueId}/matchup`);

    let html = "";

    html += `<div class="card"><h2>Week ${matchup.week || ""} Scoreboard</h2>`;
    if (matchup.message) {
      html += `<p>${matchup.message}</p>`;
    } else {
      html += `
        <p class="hint">Updates live once games kick off - 0 just means they haven't started.</p>
        <div class="matchup-row">
          <span>${matchup.me.team_name} <span class="hint">(you)</span></span>
          <span class="score">${matchup.me.points} pts</span>
        </div>
        <div class="matchup-row">
          <span>${matchup.opponent.team_name}</span>
          <span class="score">${matchup.opponent.points} pts</span>
        </div>`;
    }
    html += "</div>";

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

const teamNeedsOutput = document.getElementById("teamNeedsOutput");

async function loadTeamNeeds(leagueId) {
  teamNeedsOutput.innerHTML = '<p class="loading">Loading...</p>';
  try {
    const data = await fetchJSON(`/api/leagues/${leagueId}/team-needs`);
    if (!data.needs.length) {
      teamNeedsOutput.innerHTML = "<p>Not enough recent stats yet to judge this.</p>";
      return;
    }
    const labelClass = { Strength: "label-strength", Weakness: "label-weakness", Average: "label-average" };
    let html = '<div class="player-list">';
    html += data.needs.map(n => `
      <div class="player-row">
        <span><strong>${n.position}</strong> <span class="${labelClass[n.label]}">${n.label}</span></span>
        <span class="pos">You: ${n.my_avg} pts/gm · League avg: ${n.league_avg}</span>
      </div>`).join("");
    html += "</div>";
    teamNeedsOutput.innerHTML = html;
  } catch (err) {
    teamNeedsOutput.innerHTML = `<p class="error">Couldn't load team needs: ${err.message}</p>`;
  }
}
