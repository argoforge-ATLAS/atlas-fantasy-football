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
  try {
    const [matchup, lineup] = await Promise.all([
      fetchJSON(`/api/leagues/${leagueId}/matchup`),
      fetchJSON(`/api/leagues/${leagueId}/lineup`),
    ]);

    let html = "";

    html += '<div class="card"><h2>This Week</h2>';
    if (matchup.message) {
      html += `<p>${matchup.message}</p>`;
    } else {
      html += `
        <div class="matchup-row">
          <span>${matchup.me.team_name}</span>
          <span class="score">${matchup.me.points}</span>
        </div>
        <div class="matchup-row">
          <span>${matchup.opponent.team_name}</span>
          <span class="score">${matchup.opponent.points}</span>
        </div>`;
    }
    html += "</div>";

    html += '<div class="card"><h2>Starters</h2>';
    html += lineup.starters.map(p => `<div class="player-row"><span>${p.name}</span></div>`).join("");
    html += "</div>";

    html += '<div class="card"><h2>Bench</h2>';
    html += lineup.bench.map(p => `<div class="player-row"><span>${p.name}</span></div>`).join("");
    html += "</div>";

    content.innerHTML = html;
  } catch (err) {
    content.innerHTML = `<p class="error">Couldn't load this league: ${err.message}</p>`;
  }
}

refreshBtn.onclick = () => activeLeagueId && loadLeagueView(activeLeagueId);

const adviceBtn = document.getElementById("adviceBtn");
const adviceOutput = document.getElementById("adviceOutput");
const notesInput = document.getElementById("notesInput");

adviceBtn.onclick = async () => {
  if (!activeLeagueId) return;
  adviceBtn.disabled = true;
  adviceBtn.textContent = "Thinking...";
  adviceOutput.innerHTML = "";
  try {
    const res = await fetch(`/api/leagues/${activeLeagueId}/advice`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ notes: notesInput.value }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    adviceOutput.innerHTML = `<p class="advice-text">${data.advice.replace(/\n/g, "<br>")}</p>`;
  } catch (err) {
    adviceOutput.innerHTML = `<p class="error">Couldn't get advice: ${err.message}</p>`;
  } finally {
    adviceBtn.disabled = false;
    adviceBtn.textContent = "Get start/sit & waiver advice";
  }
};

loadLeagues().catch(err => {
  content.innerHTML = `<p class="error">Couldn't load leagues: ${err.message}</p>`;
});
