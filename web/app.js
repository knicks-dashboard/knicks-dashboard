// 대시보드는 /api/snapshot 만 읽는다. 리플레이인지 라이브인지는 status.mode 로만 구분한다.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (v) => (v == null ? "—" : (v * 100).toFixed(1) + "%");

const FF_ROWS = [
  { key: "efg", label: "eFG%", higherIsBetter: true },
  { key: "tov", label: "턴오버율", higherIsBetter: false },
  { key: "oreb", label: "공격리바운드율", higherIsBetter: true },
  { key: "ftr", label: "자유투 획득률 (FTA/FGA)", higherIsBetter: true },
];

let snap = null;
let seeking = false;
let controlsBuilt = false;

function teamColor(tri) {
  return tri === snap.game.focus ? "var(--focus)" : "var(--opp)";
}
function swatch(tri) {
  return `<span class="swatch" style="background:${teamColor(tri)}"></span>`;
}

async function poll() {
  try {
    const res = await fetch("/api/snapshot", { cache: "no-store" });
    snap = await res.json();
    render();
  } catch (e) {
    $("mode").textContent = "연결 끊김";
  }
  const live = snap && snap.status.mode === "live";
  setTimeout(poll, live ? 3000 : 400);
}

async function control(cmd) {
  await fetch("/api/control", { method: "POST", body: JSON.stringify(cmd) });
}

function render() {
  const { game, status } = snap;
  document.title = `${game.away.tricode} ${snap.score[game.away.tricode]} – ${snap.score[game.home.tricode]} ${game.home.tricode}`;
  $("title").textContent = game.title;
  renderHeader();
  renderControls(status);
  renderChart();
  renderWinProb();
  renderUnusual();
  renderAlerts();
  renderLinescore();
  renderRuns();
  renderFourFactors();
  renderBox("away");
  renderBox("home");
  renderPlays();
}

function renderHeader() {
  const { game, status } = snap;
  for (const side of ["away", "home"]) {
    const el = $(`${side}-team`);
    const tri = game[side].tricode;
    el.querySelector(".tri").textContent = tri;
    el.querySelector(".score").textContent = snap.score[tri];
    el.querySelector(".swatch").style.background = teamColor(tri);
  }
  $("period").textContent = status.final ? "경기 종료" : snap.clock.label;
  $("clock").textContent = snap.clock.remaining;
  const mode = $("mode");
  mode.textContent = status.mode === "live" ? "LIVE" : `리플레이 · ${status.speed}배속`;
  mode.className = "badge" + (status.mode === "live" ? " live" : "");
  const run = snap.current_run;
  $("current-run").innerHTML = run && run.pts >= 6
    ? `${swatch(run.team)} ${esc(run.team)} ${run.pts}-0 연속 득점 중` : "";
}

function renderControls(status) {
  const isReplay = status.mode === "replay";
  $("controls").hidden = !isReplay;
  $("live-info").hidden = isReplay;
  if (!isReplay) {
    $("live-info").textContent = `라이브 데이터 · ${status.poll_sec}초마다 갱신` + (status.error ? ` · 마지막 갱신 실패: ${status.error}` : "");
    return;
  }
  if (!controlsBuilt) {
    $("speeds").innerHTML = status.speeds.map((s) => `<button class="btn" data-speed="${s}">${s}×</button>`).join("");
    $("speeds").addEventListener("click", (e) => {
      const s = e.target.dataset.speed;
      if (s) control({ action: "speed", value: Number(s) });
    });
    $("play").addEventListener("click", () => control({ action: snap.status.playing ? "pause" : "play" }));
    $("restart").addEventListener("click", () => control({ action: "restart" }));
    const seek = $("seek");
    seek.max = status.total;
    seek.addEventListener("input", () => { seeking = true; });
    seek.addEventListener("change", async () => { await control({ action: "seek", value: Number(seek.value) }); seeking = false; });
    $("period-jumps").innerHTML = status.periods.map((p) =>
      `<button data-t="${p.start}" style="left:${(p.start / status.total) * 100}%">${p.label}</button>`).join("");
    $("period-jumps").addEventListener("click", (e) => {
      const t = e.target.dataset.t;
      if (t != null) control({ action: "seek", value: Number(t) });
    });
    controlsBuilt = true;
  }
  $("play").textContent = status.playing ? "⏸ 일시정지" : "▶ 재생";
  for (const b of $("speeds").children) b.classList.toggle("on", Number(b.dataset.speed) === status.speed);
  if (!seeking) $("seek").value = status.elapsed;
}

// ---- 점수 차 흐름 그래프 ----
const PERIOD_SEC = 720, OT_SEC = 300;
function periodStart(p) { return p <= 4 ? (p - 1) * PERIOD_SEC : 4 * PERIOD_SEC + (p - 5) * OT_SEC; }

function renderChart() {
  const svg = $("chart");
  const W = svg.clientWidth || 900, H = 300;
  const pad = { l: 36, r: 12, t: 22, b: 24 };
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const { game, status, margin, runs } = snap;
  const lastPeriod = Math.max(4, snap.clock.period);
  const total = status.total || periodStart(lastPeriod + 1);
  const maxAbs = Math.max(10, ...margin.map((d) => Math.abs(d.margin)));
  const yMax = Math.ceil(maxAbs / 5) * 5;
  const x = (t) => pad.l + (t / total) * (W - pad.l - pad.r);
  const y = (v) => pad.t + ((yMax - v) / (2 * yMax)) * (H - pad.t - pad.b);
  const now = snap.clock.elapsed;

  // 계단형 선: 득점이 날 때만 점수 차가 바뀐다
  let d = `M${x(0)},${y(0)}`;
  let prev = 0;
  for (const p of margin) { d += ` L${x(p.t)},${y(prev)} L${x(p.t)},${y(p.margin)}`; prev = p.margin; }
  d += ` L${x(now)},${y(prev)}`;
  const area = `${d} L${x(now)},${y(0)} Z`;

  let out = `<defs>
    <clipPath id="above"><rect x="0" y="0" width="${W}" height="${y(0)}"/></clipPath>
    <clipPath id="below"><rect x="0" y="${y(0)}" width="${W}" height="${H}"/></clipPath>
  </defs>`;
  // 런 구간 배경 (라벨이 겹치면 생략 — 전체 목록은 런 구간 카드에 있다)
  let labelRight = -Infinity;
  for (const r of runs) {
    const x0 = x(r.start_t), x1 = Math.max(x(r.end_t), x0 + 3);
    const soft = r.team === game.focus ? "var(--focus-soft)" : "var(--opp-soft)";
    out += `<rect x="${x0}" y="${pad.t}" width="${x1 - x0}" height="${H - pad.t - pad.b}" fill="${soft}"/>`;
    const text = `${r.team} ${r.pts}-${r.opp_pts}`;
    const half = text.length * 3.4, cx = (x0 + x1) / 2;
    if (cx - half > labelRight + 6) {
      out += `<text class="run-label" x="${cx}" y="${pad.t - 6}" text-anchor="middle">${esc(text)}</text>`;
      labelRight = cx + half;
    }
  }
  // 격자와 축
  for (let v = -yMax; v <= yMax; v += yMax >= 20 ? 10 : 5) {
    out += `<line x1="${pad.l}" x2="${W - pad.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)" stroke-width="1"/>`;
    out += `<text x="${pad.l - 6}" y="${y(v) + 4}" text-anchor="end">${v > 0 ? "+" + v : v}</text>`;
  }
  out += `<line x1="${pad.l}" x2="${W - pad.r}" y1="${y(0)}" y2="${y(0)}" stroke="var(--muted)" stroke-width="1"/>`;
  for (let p = 1; p <= lastPeriod; p++) {
    const px = x(periodStart(p));
    if (p > 1) out += `<line x1="${px}" x2="${px}" y1="${pad.t}" y2="${H - pad.b}" stroke="var(--line)" stroke-dasharray="3 3"/>`;
    const span = p <= 4 ? PERIOD_SEC : OT_SEC;
    out += `<text x="${x(periodStart(p) + span / 2)}" y="${H - 6}" text-anchor="middle">${p <= 4 ? "Q" + p : "OT" + (p - 4)}</text>`;
  }
  // 리드 영역
  out += `<path d="${area}" fill="var(--focus)" opacity=".22" clip-path="url(#above)"/>`;
  out += `<path d="${area}" fill="var(--opp)" opacity=".22" clip-path="url(#below)"/>`;
  out += `<path d="${d}" fill="none" stroke="var(--focus)" stroke-width="2" clip-path="url(#above)"/>`;
  out += `<path d="${d}" fill="none" stroke="var(--opp)" stroke-width="2" clip-path="url(#below)"/>`;
  if (status.mode === "replay" && !status.final) {
    out += `<line x1="${x(now)}" x2="${x(now)}" y1="${pad.t}" y2="${H - pad.b}" stroke="var(--ink-2)" stroke-width="1"/>`;
  }
  out += `<line id="crosshair" y1="${pad.t}" y2="${H - pad.b}" stroke="var(--ink-2)" stroke-width="1" stroke-dasharray="2 2" visibility="hidden"/>`;
  out += `<circle id="hover-dot" r="4" fill="var(--surface)" stroke="var(--ink)" stroke-width="2" visibility="hidden"/>`;
  out += `<rect id="hit" x="${pad.l}" y="0" width="${W - pad.l - pad.r}" height="${H}" fill="transparent"/>`;
  svg.innerHTML = out;

  $("legend").innerHTML =
    `<span>${swatch(game.focus)}${esc(game.focus)} 리드 (위)</span><span>${swatch(game.opponent)}${esc(game.opponent)} 리드 (아래)</span>`;

  const hit = $("hit"), tip = $("tooltip"), cross = $("crosshair"), dot = $("hover-dot");
  hit.onmouseleave = () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); };
  hit.onmousemove = (ev) => {
    const box = svg.getBoundingClientRect();
    const mx = (ev.clientX - box.left) * (W / box.width);
    const t = ((mx - pad.l) / (W - pad.l - pad.r)) * total;
    let p = null;
    for (const m of margin) { if (m.t <= t) p = m; else break; }
    if (!p) { hit.onmouseleave(); return; }
    cross.setAttribute("x1", mx); cross.setAttribute("x2", mx); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", x(p.t)); dot.setAttribute("cy", y(p.margin)); dot.setAttribute("visibility", "visible");
    const lead = p.margin === 0 ? "동점" : p.margin > 0 ? `${game.focus} +${p.margin}` : `${game.opponent} +${-p.margin}`;
    tip.innerHTML = `<div class="t-head">${esc(p.label)}</div>
      <div class="t-score">${esc(game.away.tricode)} ${p.away} – ${p.home} ${esc(game.home.tricode)} · ${esc(lead)}</div>
      <div>${esc(p.desc)}</div>`;
    tip.hidden = false;
    const left = (ev.clientX - box.left) + 14;
    tip.style.left = Math.min(left, box.width - tip.offsetWidth - 4) + "px";
    tip.style.top = Math.max(0, (ev.clientY - box.top) - tip.offsetHeight - 10) + "px";
  };
}

function renderLinescore() {
  const { game, linescore, baseline } = snap;
  const order = [game.away.tricode, game.home.tricode];
  const heads = linescore.map((q) => `<th>${q.label}</th>`).join("");
  let html = `<tr><th></th>${heads}<th>합계</th></tr>`;
  for (const tri of order) {
    html += `<tr><td>${swatch(tri)} ${esc(tri)}</td>${linescore.map((q) => `<td>${q[tri]}</td>`).join("")}<td><b>${snap.score[tri]}</b></td></tr>`;
  }
  if (baseline) {
    const byQ = Object.fromEntries(baseline.quarters.map((q) => [q.QUARTER, q]));
    html += `<tr><td class="muted">${esc(game.focus)} 시즌 평균 득점</td>${linescore.map((q) => `<td class="muted">${byQ[q.label] ? byQ[q.label].PTS.toFixed(1) : "—"}</td>`).join("")}<td class="muted">${baseline.per_game.PTS.toFixed(1)}</td></tr>`;
    html += `<tr><td class="muted">${esc(game.focus)} 시즌 평균 실점</td>${linescore.map((q) => `<td class="muted">${byQ[q.label] ? byQ[q.label].OPP_PTS.toFixed(1) : "—"}</td>`).join("")}<td class="muted">${baseline.per_game.OPP_PTS.toFixed(1)}</td></tr>`;
    $("linescore-note").textContent = `기준값: ${baseline.season} ${baseline.season_type === "Regular Season" ? "정규시즌" : baseline.season_type} 닉스 평균 (1단계 baseline)`;
  }
  $("linescore").innerHTML = html;
}

function renderRuns() {
  const runs = snap.runs;
  $("runs").innerHTML = runs.length
    ? runs.map((r) => `<li>${swatch(r.team)}<span class="run-score">${esc(r.team)} ${r.pts}-${r.opp_pts}</span><span class="run-time">${esc(r.start_label)} → ${esc(r.end_label)}</span></li>`).join("")
    : `<li class="empty">아직 런 구간이 없습니다.</li>`;
}

function deltaCell(game, base, higherIsBetter) {
  if (game == null || base == null) return `<td class="muted">—</td>`;
  const diff = (game - base) * 100;
  if (Math.abs(diff) < 0.05) return `<td class="delta">±0.0</td>`;
  const better = higherIsBetter ? diff > 0 : diff < 0;
  return `<td class="delta ${better ? "good" : "bad"}">${diff > 0 ? "▲" : "▼"} ${Math.abs(diff).toFixed(1)}%p ${better ? "좋음" : "나쁨"}</td>`;
}

function renderFourFactors() {
  const { game, four_factors: ff, baseline } = snap;
  const f = game.focus, o = game.opponent;
  const bf = baseline?.four_factors?.[f], bo = baseline?.four_factors?.[o];
  $("ff-sub").textContent = baseline ? `기준값: ${baseline.season} 정규시즌 닉스 평균 · ${o} 칸은 닉스가 시즌 평균 허용한 수치와 비교` : "";
  let html = `<tr><th>지표</th><th>${swatch(f)} ${esc(f)} 이 경기</th><th>${esc(f)} 시즌</th><th>차이</th>
    <th>${swatch(o)} ${esc(o)} 이 경기</th><th>${esc(f)} 시즌 허용</th><th>차이 (${esc(f)} 수비 기준)</th></tr>`;
  for (const r of FF_ROWS) {
    html += `<tr><td>${r.label}</td>
      <td><b>${pct(ff[f][r.key])}</b></td><td class="muted">${pct(bf?.[r.key])}</td>${deltaCell(ff[f][r.key], bf?.[r.key], r.higherIsBetter)}
      <td><b>${pct(ff[o][r.key])}</b></td><td class="muted">${pct(bo?.[r.key])}</td>${deltaCell(ff[o][r.key], bo?.[r.key], !r.higherIsBetter)}</tr>`;
  }
  $("four-factors").innerHTML = html;
}

function renderBox(side) {
  const { game, box } = snap;
  const tri = game[side].tricode;
  const b = box[tri];
  const showSeason = tri === game.focus && snap.baseline;
  $(`box-${side}-title`).innerHTML = `${swatch(tri)} ${esc(game[side].city)} ${esc(game[side].name)}`;
  const cols = ["MIN", "PTS", ...(showSeason ? ["시즌 PPG"] : []), "REB", "AST", "STL", "BLK", "TOV", "PF", "FG", "3P", "FT", "+/-"];
  let html = `<tr><th>선수</th>${cols.map((c) => `<th>${c}</th>`).join("")}</tr>`;
  let benchMarked = false;
  for (const p of b.players) {
    const cls = !p.starter && !benchMarked ? ' class="bench-start"' : "";
    if (!p.starter) benchMarked = true;
    html += `<tr${cls}><td class="name"><span class="oncourt ${p.oncourt ? "on" : ""}" title="${p.oncourt ? "코트 위" : "벤치"}"></span>${esc(p.name)}</td>
      <td>${p.min}</td><td><b>${p.pts}</b></td>${showSeason ? `<td class="muted">${p.season_ppg != null ? p.season_ppg.toFixed(1) : "—"}</td>` : ""}
      <td>${p.reb}</td><td>${p.ast}</td><td>${p.stl}</td><td>${p.blk}</td><td>${p.tov}</td><td>${p.pf}</td>
      <td>${p.fgm}-${p.fga}</td><td>${p.fg3m}-${p.fg3a}</td><td>${p.ftm}-${p.fta}</td><td>${p.pm > 0 ? "+" + p.pm : p.pm}</td></tr>`;
  }
  const t = b.totals;
  html += `<tr class="total"><td>합계</td><td></td><td>${t.pts}</td>${showSeason ? "<td></td>" : ""}<td>${t.reb}</td><td>${t.ast}</td><td>${t.stl}</td><td>${t.blk}</td><td>${t.tov}</td><td>${t.pf}</td>
    <td>${t.fgm}-${t.fga}</td><td>${t.fg3m}-${t.fg3a}</td><td>${t.ftm}-${t.fta}</td><td></td></tr>`;
  $(`box-${side}`).innerHTML = html;
}

function renderPlays() {
  const { game } = snap;
  $("plays").innerHTML = snap.plays.filter((p) => p.desc).map((p) => `<li class="${p.scoring ? "scoring" : ""}">
    <span class="p-time">${esc(p.label)}</span>
    <span class="p-team">${p.team ? swatch(p.team) + esc(p.team) : ""}</span>
    <span class="p-desc">${esc(p.desc)}</span>
    <span class="p-score">${p.scoring ? `${p.away}-${p.home}` : ""}</span></li>`).join("");
}

// ---- 알림 ----
const TOAST_WINDOW = 120;   // 경기 시간 기준 최근 2분 안에 생긴 알림만 띄운다 (시점 이동 시 폭주 방지)
const TOAST_MS = 9000;
const MAX_TOASTS = 3;
const seenAlerts = new Set();
const openToasts = new Map();

function alertKind(a) {
  if (a.kind === "run") return { tag: "런", icon: "▲" };
  return { tag: a.severe ? "파울 위험" : "파울", icon: "⚠" };
}

function renderAlerts() {
  const { alerts, clock } = snap;
  const ids = new Set(alerts.map((a) => a.id));
  for (const id of [...seenAlerts]) if (!ids.has(id)) seenAlerts.delete(id);  // 뒤로 이동하면 다시 알릴 수 있게
  for (const [id, el] of openToasts) if (!ids.has(id)) closeToast(id);

  for (const a of alerts) {
    const open = openToasts.get(a.id);
    if (open) open.querySelector(".t-text").textContent = a.text;  // 진행 중인 런은 점수 갱신
    if (seenAlerts.has(a.id)) continue;
    seenAlerts.add(a.id);
    if (clock.elapsed - a.t <= TOAST_WINDOW) showToast(a);
  }

  $("alert-log").innerHTML = alerts.length
    ? alerts.slice().reverse().map((a) => {
        const k = alertKind(a);
        return `<li><span class="a-time">${esc(a.label)}</span><span class="a-kind">${swatch(a.team)} ${k.tag}</span><span><span class="a-icon">${k.icon}</span>${esc(a.text)}${a.active ? " · 진행 중" : ""}</span></li>`;
      }).join("")
    : `<li class="empty">아직 알림이 없습니다.</li>`;
}

function showToast(a) {
  const k = alertKind(a);
  const el = document.createElement("div");
  el.className = `toast ${a.kind}${a.severe ? " severe" : ""}`;
  el.style.setProperty("--team-color", teamColor(a.team));
  el.innerHTML = `<span class="t-kind">${k.icon} ${k.tag}</span>
    <div class="t-body"><div class="t-text">${esc(a.text)}</div><div class="t-when">${swatch(a.team)} ${esc(a.team)} · ${esc(a.label)}</div></div>
    <button aria-label="닫기">×</button>`;
  el.querySelector("button").onclick = () => closeToast(a.id);
  $("toasts").prepend(el);
  openToasts.set(a.id, el);
  setTimeout(() => closeToast(a.id), TOAST_MS);
  while (openToasts.size > MAX_TOASTS) closeToast(openToasts.keys().next().value);
}

function closeToast(id) {
  const el = openToasts.get(id);
  if (!el) return;
  el.remove();
  openToasts.delete(id);
}

// ---- 승리 확률 ----
function renderWinProb() {
  const { game, status, win_prob: wp } = snap;
  const svg = $("wp-chart");
  const W = svg.clientWidth || 500, H = 200;
  const pad = { l: 40, r: 10, t: 10, b: 22 };
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const lastPeriod = Math.max(4, snap.clock.period);
  const total = status.total || periodStart(lastPeriod + 1);
  const x = (t) => pad.l + (t / total) * (W - pad.l - pad.r);
  const y = (p) => pad.t + (1 - p) * (H - pad.t - pad.b);

  $("wp-title").innerHTML = `${swatch(game.focus)} ${esc(game.focus)} 승리 확률`;
  $("wp-now").textContent = (wp.current * 100).toFixed(0) + "%";
  $("wp-note").textContent = `단순 모델: 현재 점수 차와 남은 시간만 사용 (경기 전체 점수 차 표준편차 ${wp.model.sigma}점, 홈 어드밴티지 ${wp.model.home_edge}점). 선수 구성·공격권·파울 상황은 반영하지 않습니다.`;

  let out = "";
  for (const p of [0, 0.25, 0.5, 0.75, 1]) {
    out += `<line x1="${pad.l}" x2="${W - pad.r}" y1="${y(p)}" y2="${y(p)}" stroke="${p === 0.5 ? "var(--muted)" : "var(--grid)"}" stroke-width="1"/>`;
    out += `<text x="${pad.l - 6}" y="${y(p) + 4}" text-anchor="end">${p * 100}%</text>`;
  }
  for (let p = 1; p <= lastPeriod; p++) {
    const px = x(periodStart(p));
    if (p > 1) out += `<line x1="${px}" x2="${px}" y1="${pad.t}" y2="${H - pad.b}" stroke="var(--line)" stroke-dasharray="3 3"/>`;
    const span = p <= 4 ? PERIOD_SEC : OT_SEC;
    out += `<text x="${x(periodStart(p) + span / 2)}" y="${H - 6}" text-anchor="middle">${p <= 4 ? "Q" + p : "OT" + (p - 4)}</text>`;
  }
  const pts = wp.series.map((d) => `${x(d.t)},${y(d.p)}`).join(" L");
  if (pts) {
    out += `<path d="M${pts} L${x(wp.series.at(-1).t)},${y(0)} L${x(0)},${y(0)} Z" fill="var(--focus)" opacity=".1"/>`;
    out += `<path d="M${pts}" fill="none" stroke="var(--focus)" stroke-width="2"/>`;
  }
  out += `<line id="wp-cross" y1="${pad.t}" y2="${H - pad.b}" stroke="var(--ink-2)" stroke-dasharray="2 2" visibility="hidden"/>`;
  out += `<circle id="wp-dot" r="4" fill="var(--surface)" stroke="var(--focus)" stroke-width="2" visibility="hidden"/>`;
  out += `<rect id="wp-hit" x="${pad.l}" y="0" width="${W - pad.l - pad.r}" height="${H}" fill="transparent"/>`;
  svg.innerHTML = out;

  const hit = $("wp-hit"), tip = $("wp-tooltip"), cross = $("wp-cross"), dot = $("wp-dot");
  hit.onmouseleave = () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); };
  hit.onmousemove = (ev) => {
    const box = svg.getBoundingClientRect();
    const mx = (ev.clientX - box.left) * (W / box.width);
    const t = ((mx - pad.l) / (W - pad.l - pad.r)) * total;
    let d = null;
    for (const s of wp.series) { if (s.t <= t) d = s; else break; }
    if (!d) { hit.onmouseleave(); return; }
    cross.setAttribute("x1", x(d.t)); cross.setAttribute("x2", x(d.t)); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", x(d.t)); dot.setAttribute("cy", y(d.p)); dot.setAttribute("visibility", "visible");
    const lead = d.margin === 0 ? "동점" : d.margin > 0 ? `${game.focus} +${d.margin}` : `${game.opponent} +${-d.margin}`;
    tip.innerHTML = `<div class="t-head">${esc(clockLabel(d.t))}</div><div class="t-score">${esc(game.focus)} 승리 확률 ${(d.p * 100).toFixed(1)}%</div><div>${esc(lead)}</div>`;
    tip.hidden = false;
    tip.style.left = Math.min((ev.clientX - box.left) + 14, box.width - tip.offsetWidth - 4) + "px";
    tip.style.top = Math.max(0, (ev.clientY - box.top) - tip.offsetHeight - 10) + "px";
  };
}

function clockLabel(t) {
  let p = 1;
  while (t >= periodStart(p + 1)) p++;
  const rem = periodStart(p) + (p <= 4 ? PERIOD_SEC : OT_SEC) - t;
  const mm = Math.floor(rem / 60), ss = Math.floor(rem % 60);
  return `${p <= 4 ? "Q" + p : "OT" + (p - 4)} ${mm}:${String(ss).padStart(2, "0")}`;
}

// ---- 오늘 평소와 다른 점 ----
function renderUnusual() {
  const u = snap.unusual;
  $("unusual").innerHTML = u.items.map((it) => {
    const dir = it.higher ? "▲ 평소보다 높음" : "▼ 평소보다 낮음";
    return `<li><div class="u-head"><span class="u-title">${esc(it.subject)} · ${esc(it.label)}</span>
      <span class="delta ${it.good ? "good" : "bad"}">${dir} · ${it.good ? "좋음" : "나쁨"}</span></div>
      <div class="u-value">오늘 <b>${esc(it.value_text)}</b> / 시즌 평균 ${esc(it.baseline_text)}</div>
      <div class="u-z">평소 경기별 편차의 ${Math.abs(it.z).toFixed(1)}배만큼 벗어남</div></li>`;
  }).join("");
  $("unusual-note").textContent = u.note || "기준: 2025-26 정규시즌 경기별 평균·표준편차. 경기 중에는 현재 페이스로 환산하고, 남은 시간이 많을수록 보수적으로 판단합니다.";
}

window.addEventListener("resize", () => { if (snap) { renderChart(); renderWinProb(); } });
poll();
