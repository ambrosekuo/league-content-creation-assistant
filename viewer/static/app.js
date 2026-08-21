const KIND_ORDER = ["portrait", "weave", "daily", "clip", "source", "other"];
const KIND_LABEL = {
  portrait: "Portraits",
  weave: "Weaves",
  daily: "Daily",
  clip: "Clips",
  source: "Source",
  other: "Other",
};

const state = {
  catalog: null,
  vod: null,
  vodId: null,
  dayKey: null,
  kind: "portrait",
  selected: null,
  where: "cloud",
  notesTimer: null,
};

const $ = (id) => document.getElementById(id);

function fmtBytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function fmtDur(sec) {
  if (!sec && sec !== 0) return "";
  const s = Math.round(Number(sec));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

function vodLabel(vod) {
  if (vod.localName) return vod.localName;
  if (vod.dayKey && vod.dayKey !== "local") return `${vod.dayKey}_${vod.vodId}`;
  return vod.vodId;
}

async function api(path, opts) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data.detail || data.stderr || res.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

function mediaUrl(video) {
  const src = video.local ? "local" : "gcs";
  const q = new URLSearchParams({
    vod: state.vodId,
    path: video.path,
    src,
  });
  if (state.dayKey && state.dayKey !== "local") q.set("day", state.dayKey);
  return `/api/media?${q}`;
}

function thumbUrl(video) {
  const q = new URLSearchParams({
    vod: state.vodId,
    path: video.path,
    src: video.local ? "local" : "gcs",
  });
  if (state.dayKey && state.dayKey !== "local") q.set("day", state.dayKey);
  return `/api/thumb?${q}`;
}

function videosOfKind(kind) {
  const all = state.vod?.videos || [];
  if (kind === "all") return all;
  return all.filter((v) => v.kind === kind);
}

function renderNav() {
  const root = $("day-list");
  root.innerHTML = "";
  for (const day of state.catalog.days || []) {
    const label = document.createElement("div");
    label.className = "day-label";
    label.textContent = day.dayKey + (day.hasDaily ? " · daily" : "");
    root.appendChild(label);
    for (const vod of day.vods) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "vod-btn" + (vod.vodId === state.vodId ? " on" : "");
      const flags = [];
      if (vod.flags?.portraits) flags.push("9:16");
      if (vod.flags?.weaves) flags.push("weave");
      if (vod.flags?.clips) flags.push("clips");
      btn.innerHTML = `<strong>${vodLabel(vod)}</strong>
        <small>${vod.title ? vod.title.slice(0, 42) : (vod.local ? "local" : "gcs")}</small>
        <span class="flags">${flags.map((f) => `<span class="flag">${f}</span>`).join("")}</span>`;
      btn.addEventListener("click", () => selectVod(vod.vodId, vod.dayKey));
      root.appendChild(btn);
    }
  }
}

function renderChips() {
  const counts = {};
  for (const v of state.vod?.videos || []) counts[v.kind] = (counts[v.kind] || 0) + 1;
  const root = $("kind-chips");
  root.innerHTML = "";
  const kinds = KIND_ORDER.filter((k) => counts[k]);
  if (!kinds.includes(state.kind) && kinds.length) state.kind = kinds[0];
  for (const kind of kinds) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = state.kind === kind ? "on" : "";
    btn.textContent = `${KIND_LABEL[kind] || kind} ${counts[kind]}`;
    btn.addEventListener("click", () => {
      state.kind = kind;
      renderChips();
      renderGrid();
    });
    root.appendChild(btn);
  }
}

function renderGrid() {
  const items = videosOfKind(state.kind);
  $("lib-count").textContent = `${items.length} file${items.length === 1 ? "" : "s"}`;
  const grid = $("grid");
  grid.innerHTML = "";
  for (const video of items) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "card";
    card.dataset.kind = video.kind;
    if (state.selected?.path === video.path) card.classList.add("on");
    if (video.review === "keep") card.classList.add("keep");
    if (video.review === "skip") card.classList.add("skip");
    const thumb = document.createElement("div");
    thumb.className = "thumb";
    if (video.local || video.gcs) thumb.style.backgroundImage = `url("${thumbUrl(video)}")`;
    const cap = document.createElement("figcaption");
    const loc = [video.local ? "local" : null, video.gcs ? "gcs" : null].filter(Boolean).join(" · ");
    cap.innerHTML = `<strong>${video.name.replace(/\.mp4$/i, "")}</strong>${fmtBytes(video.size)} · ${loc}`;
    card.appendChild(thumb);
    card.appendChild(cap);
    card.addEventListener("click", () => play(video));
    grid.appendChild(card);
  }
}

function play(video) {
  if (video.kind === "source" && !video.local) {
    $("job-out").classList.remove("hidden");
    $("job-out").textContent = "source.* is not streamed from GCS. Use Pull from GCS first.";
    state.selected = video;
    renderGrid();
    return;
  }
  state.selected = video;
  const player = $("player");
  const frame = document.querySelector(".player-frame");
  frame.dataset.kind = video.kind;
  player.src = mediaUrl(video);
  player.play().catch(() => {});
  $("player-empty").classList.add("hidden");
  $("now-file").textContent = `${video.path}  ·  ${fmtBytes(video.size)}`;
  renderGrid();
}

function renderNow() {
  const vod = state.vod;
  $("now-title").textContent = vod.title || vodLabel(vod);
  const bits = [
    vod.dayKey,
    vod.duration ? fmtDur(vod.duration) : null,
    vod.local ? "disk" : null,
    vod.gcs ? "gcs" : null,
  ].filter(Boolean);
  $("now-meta").textContent = bits.join(" · ");
  $("notes").value = vod.notes || "";
  $("notes-status").textContent = "";
}

function renderJobs() {
  const root = $("job-list");
  root.innerHTML = "";
  for (const job of state.vod?.jobs || []) {
    if (job.surface === "review") continue;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.innerHTML = `${job.label}<small>${job.help}</small>`;
    btn.addEventListener("click", () => runJob(job.id));
    root.appendChild(btn);
  }
}

async function selectVod(vodId, dayKey) {
  state.vodId = vodId;
  state.dayKey = dayKey;
  state.selected = null;
  $("player").removeAttribute("src");
  $("player-empty").classList.remove("hidden");
  $("now-file").textContent = "";
  const q = dayKey ? `?day=${encodeURIComponent(dayKey)}` : "";
  state.vod = await api(`/api/vods/${encodeURIComponent(vodId)}${q}`);
  const portraits = (state.vod.videos || []).filter((v) => v.kind === "portrait");
  state.kind = portraits.length ? "portrait" : ((state.vod.videos || [])[0]?.kind || "portrait");
  renderNav();
  renderNow();
  renderChips();
  renderGrid();
  renderJobs();
  const review = $("btn-review");
  if (review) {
    const day = state.dayKey && state.dayKey !== "local" ? state.dayKey : null;
    review.href = day
      ? `/review/${encodeURIComponent(day)}/${encodeURIComponent(state.vodId)}`
      : `/review/${encodeURIComponent(state.vodId)}`;
    review.classList.remove("hidden");
  }
  const first = videosOfKind(state.kind)[0];
  if (first) play(first);
}

function findCatalogVod(vodId) {
  for (const day of state.catalog?.days || []) {
    const hit = (day.vods || []).find((v) => v.vodId === vodId);
    if (hit) return hit;
  }
  return null;
}

async function loadCatalog() {
  state.catalog = await api("/api/catalog?gcs=false");
  $("source-line").textContent = "local data/";
  renderNav();
  const first = state.catalog.days?.[0]?.vods?.[0];
  if (first && !state.vodId) await selectVod(first.vodId, first.dayKey);
  else if (state.vodId) await selectVod(state.vodId, state.dayKey);
  try {
    const remote = await api("/api/catalog");
    state.catalog = remote;
    const src = [];
    if (remote.gcs) src.push(`gs://${remote.bucket} (stream, no download)`);
    else src.push("local data/ only");
    if (remote.gcsError) src.push(`gcs: ${remote.gcsError}`);
    $("source-line").textContent = src.join(" · ");
    renderNav();
    const match = findCatalogVod(state.vodId);
    if (match) await selectVod(match.vodId, match.dayKey);
  } catch (err) {
    $("source-line").textContent = `local data/ · gcs failed: ${err.message}`;
  }
}

function splitArgs(raw) {
  const out = [];
  const re = /"([^"]*)"|'([^']*)'|(\S+)/g;
  let m;
  while ((m = re.exec(raw))) out.push(m[1] || m[2] || m[3]);
  return out;
}

async function runJob(jobId) {
  const out = $("job-out");
  out.classList.remove("hidden");
  out.textContent = "running…";
  try {
    const result = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({
        job: jobId,
        vodId: state.vodId || "",
        dayKey: state.dayKey || "",
        extra: splitArgs($("extra-args").value.trim()),
        where: state.where,
      }),
    });
    const lines = [result.commandLine, result.ok ? "ok" : "failed"];
    if (result.log) lines.push(`log ${result.log}`);
    if (result.console) lines.push(result.console);
    if (result.stdout) lines.push(result.stdout.trim());
    if (result.stderr) lines.push(result.stderr.trim());
    out.textContent = lines.filter(Boolean).join("\n\n");
  } catch (err) {
    out.textContent = String(err.message || err);
  }
}

async function saveNotes() {
  if (!state.vodId) return;
  const notes = $("notes").value;
  await api(`/api/vods/${encodeURIComponent(state.vodId)}/notes`, {
    method: "PUT",
    body: JSON.stringify({ notes }),
  });
  $("notes-status").textContent = "saved";
}

async function setReview(status) {
  if (!state.vodId || !state.selected) return;
  const rec = await api(`/api/vods/${encodeURIComponent(state.vodId)}/review`, {
    method: "PUT",
    body: JSON.stringify({ path: state.selected.path, status }),
  });
  for (const video of state.vod.videos) {
    video.review = rec.files?.[video.path] || null;
  }
  if (state.selected) state.selected.review = rec.files?.[state.selected.path] || null;
  renderGrid();
}

function step(delta) {
  const items = videosOfKind(state.kind);
  if (!items.length) return;
  const idx = Math.max(0, items.findIndex((v) => v.path === state.selected?.path));
  const next = items[(idx + delta + items.length) % items.length];
  play(next);
}

$("btn-refresh").addEventListener("click", () => loadCatalog());
$("btn-keep").addEventListener("click", () => setReview("keep"));
$("btn-skip").addEventListener("click", () => setReview("skip"));
$("notes").addEventListener("input", () => {
  $("notes-status").textContent = "saving…";
  clearTimeout(state.notesTimer);
  state.notesTimer = setTimeout(() => saveNotes().catch((e) => {
    $("notes-status").textContent = e.message;
  }), 400);
});
$("where-seg").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-where]");
  if (!btn) return;
  state.where = btn.dataset.where;
  for (const el of $("where-seg").querySelectorAll("button")) {
    el.classList.toggle("on", el === btn);
  }
});
$("btn-pull").addEventListener("click", async () => {
  if (!state.selected) return;
  $("job-out").classList.remove("hidden");
  $("job-out").textContent = "pulling from GCS…";
  try {
    const res = await api(`/api/vods/${encodeURIComponent(state.vodId)}/pull`, {
      method: "POST",
      body: JSON.stringify({ path: state.selected.path }),
    });
    $("job-out").textContent = `saved ${res.path} (${fmtBytes(res.bytes)})`;
    await selectVod(state.vodId, state.dayKey);
  } catch (err) {
    $("job-out").textContent = String(err.message || err);
  }
});
$("btn-delete").addEventListener("click", async () => {
  if (!state.selected) return;
  if (!confirm(`Delete local file?\n${state.selected.path}`)) return;
  await api(`/api/vods/${encodeURIComponent(state.vodId)}/delete`, {
    method: "POST",
    body: JSON.stringify({ path: state.selected.path, local: true, gcs: false }),
  });
  await selectVod(state.vodId, state.dayKey);
});

document.addEventListener("keydown", (ev) => {
  const tag = ev.target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA") return;
  if (ev.key === "j" || ev.key === "ArrowDown") step(1);
  if (ev.key === "k" || ev.key === "ArrowUp") step(-1);
  if (ev.key === "1") setReview("keep");
  if (ev.key === "2") setReview("skip");
  if (ev.key === " ") {
    ev.preventDefault();
    const p = $("player");
    if (p.paused) p.play();
    else p.pause();
  }
});

loadCatalog().catch((err) => {
  $("source-line").textContent = String(err.message || err);
});
