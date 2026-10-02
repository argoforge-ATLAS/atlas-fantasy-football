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

const MATCHUP_LABEL_CLASS = {
  "Tough matchup": "label-weakness",
  "Average matchup": "label-average",
  "Good matchup": "label-strength",
};

async function loadLeagueView(leagueId) {
  content.innerHTML = '<p class="loading">Loading...</p>';
  loadRecommendations(leagueId);
  loadTeamNeeds(leagueId);
  try {
    const data = await fetchJSON(`/api/leagues/${leagueId}/matchups`);

    let html = `<div class="card"><h2>Week ${data.week || ""} Matchups</h2>`;
    html += '<p class="hint">Your roster\'s real-life opponents this week, and how tough each defense has been against that position recently.</p>';

    const withMatchup = data.players.filter(p => p.opponent);
    if (!withMatchup.length) {
      html += "<p>No matchup data yet - check back once the week's schedule is set.</p>";
    } else {
      html += '<div class="player-list">';
      html += withMatchup.map(p => {
        const labelClass = MATCHUP_LABEL_CLASS[p.matchup_label] || "label-average";
        const label = p.matchup_label
          ? `<span class="${labelClass}">${p.matchup_label}</span>`
          : '<span class="hint">not enough data yet</span>';
        return `
          <div class="player-row">
            <span><strong>${p.name}</strong></span>
            <span class="pos">${p.position} vs ${p.opponent}</span>
            <span class="hint">${label}</span>
          </div>`;
      }).join("");
      html += "</div>";
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
      const matchup = w.opponent
        ? ` · vs ${w.opponent}${w.matchup_label ? " (" + w.matchup_label + ")" : ""}`
        : "";
      return `<div class="player-row"><span>${w.name}</span><span class="pos">${w.position || "?"} · ${prod} · ${fc}${matchup}</span></div>`;
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
