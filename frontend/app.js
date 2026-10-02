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

const MATCHUP_CHIP_CLASS = {
  "Tough matchup": "tough",
  "Average matchup": "average",
  "Good matchup": "good",
};

// A red "OUT"/"Questionable"/etc. badge whenever Sleeper reports a
// non-healthy status - shown everywhere a player's name appears, so
// an injured guy is never silently recommended.
function injuryBadge(player) {
  if (!player.injury_status) return "";
  const cls = player.bad_injury ? "label-weakness" : "label-average";
  return ` <span class="${cls}">${player.injury_status}</span>`;
}

// Renders the next few weeks' opponents as small colored chips -
// green = good matchup, red = tough, gray = average or unknown yet.
function matchupChips(upcoming) {
  if (!upcoming || !upcoming.length) return "";
  const chips = upcoming.map(u => {
    const cls = MATCHUP_CHIP_CLASS[u.matchup_label] || "unknown";
    const text = u.opponent || "BYE";
    return `<span class="matchup-chip ${cls}" title="Week ${u.week}${u.matchup_label ? ': ' + u.matchup_label : ''}">${text}</span>`;
  }).join("");
  return `<span class="matchup-chips">${chips}</span>`;
}

async function loadLeagueView(leagueId) {
  content.innerHTML = '<p class="loading">Loading...</p>';
  loadRecommendations(leagueId);
  loadDropCandidates(leagueId);
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
        const prod = p.recent_avg_points !== null ? `${p.recent_avg_points} pts/gm` : "no recent production";
        return `
          <div class="player-row">
            <span><strong>${p.name}</strong>${injuryBadge(p)}</span>
            <span class="pos">${p.position} vs ${p.opponent} · ${prod}</span>
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
      const chips = matchupChips(w.upcoming_matchups);
      return `<div class="player-row"><span>${w.name}${injuryBadge(w)}</span><span class="pos">${w.position || "?"} · ${prod} · ${fc}</span>${chips}</div>`;
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

const dropCandidatesOutput = document.getElementById("dropCandidatesOutput");

async function loadDropCandidates(leagueId) {
  dropCandidatesOutput.innerHTML = '<p class="loading">Loading...</p>';
  try {
    const data = await fetchJSON(`/api/leagues/${leagueId}/drop-candidates`);
    if (!data.candidates.length) {
      dropCandidatesOutput.innerHTML = "<p>Nothing standing out as droppable right now.</p>";
      return;
    }
    let html = "";
    if (data.enforce_keeper_rule) {
      html += '<p class="hint">Keeper-eligible players are flagged - dropping them costs that eligibility for good.</p>';
    }
    html += '<div class="player-list">';
    html += data.candidates.map(c => {
      const prod = c.recent_avg_points !== null ? `${c.recent_avg_points} pts/gm` : "no recent production";
      const keeperBadge = c.keeper_eligible ? ' <span class="label-keeper">Keeper eligible</span>' : "";
      const verdictClass = c.verdict === "Hold - role growing" ? "label-strength" : "label-weakness";
      let opportunity = "";
      if (c.opportunity_pct !== null && c.opportunity_pct !== undefined) {
        const trendText = c.opportunity_trend ? ` (${c.opportunity_trend.toLowerCase()})` : "";
        opportunity = ` · ${c.opportunity_pct}% share${trendText}`;
      }
      return `
        <div class="player-row">
          <span><strong>${c.name}</strong>${keeperBadge}</span>
          <span class="pos">${c.position || "?"} · ${prod}${opportunity}</span>
          <span class="${verdictClass}">${c.verdict}</span>
        </div>`;
    }).join("");
    html += "</div>";
    dropCandidatesOutput.innerHTML = html;
  } catch (err) {
    dropCandidatesOutput.innerHTML = `<p class="error">Couldn't load drop candidates: ${err.message}</p>`;
  }
}

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
