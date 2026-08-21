const RATING_LABEL = {
  reject: "rejected",
  keep: "keep",
  excellent: "excellent",
  godly: "godly",
  manual_edit: "manual edit",
};

const REVEAL_LABEL = /Mac|iPhone|iPad|iPod/i.test(navigator.platform || "")
  ? "Show in Finder"
  : /Win/i.test(navigator.platform || "")
    ? "Show in Explorer"
    : "Show file location";

const JOB_PROGRESS = {
  "review-stitch": { verb: "Stitching…", hint: "Stitching pick games. Refresh is fine — this stays until it finishes." },
  "review-portraits": { verb: "Making portraits…", hint: "Rendering dry 9:16 portraits. Refresh is fine — this stays until it finishes." },
  "review-decorate": { verb: "Decorating…", hint: "Decorating portraits. Refresh is fine — this stays until it finishes." },
  "review-music": { verb: "Mixing…", hint: "Mixing music onto decorated portraits. Refresh is fine — this stays until it finishes." },
  "review-post": { verb: "Posting…", hint: "Uploading shorts. Refresh is fine — this stays until it finishes." },
};

const state = {
  vodId: null,
  dayKey: null,
  payload: null,
  catalog: null,
  filter: "all",
  classFilter: "all",
  view: "clips",
  source: "all",
  game: "all",
  event: "all",
  matchup: "",
  index: 0,
  playerOpen: false,
  playDay: false,
  autoAdvance: true,
  speed: 1,
  trimIn: null,
  trimOut: null,
  trimming: false,
  saving: Promise.resolve(),
  syncing: false,
  syncAbort: null,
  classifying: false,
  classifyingMode: null,
  editingHook: false,
  editingHookSource: "rules",
  generatingTitles: false,
  generatingTitleId: "",
  titleContext: {},
  titleDraft: {},
  titleDraftFocus: "",
  titleReprompt: {},
  titlePick: {},
  jobRunning: false,
  jobLabel: "",
  jobPosting: false,
  mixTargetId: "",
  mixStatus: "",
  syncDone: 0,
  syncTotal: 0,
  musicTrack: localStorage.getItem("reviewMusicTrack") || "",
  musicPreview: { trackId: "", playing: false },
  postPlatforms: {
    youtube: localStorage.getItem("reviewPostYoutube") !== "0",
    tiktok: localStorage.getItem("reviewPostTiktok") !== "0",
  },
  editingPostId: "",
};

const $ = (id) => document.getElementById(id);

function parseRoute() {
  const parts = location.pathname.replace(/\/+$/, "").split("/").filter(Boolean);
  const params = new URLSearchParams(location.search);
  let dayKey = null;
  let vodId = null;
  if (parts[0] === "review" && parts.length === 2) vodId = parts[1];
  if (parts[0] === "review" && parts.length >= 3) {
    dayKey = parts[1];
    vodId = parts[2];
  }
  return {
    vodId,
    dayKey,
    filter: params.get("filter") || "all",
    classFilter: params.get("class") || "all",
    view: params.get("view") || "clips",
    source: params.get("source") || "all",
    game: params.get("game") || "all",
    event: params.get("event") || "all",
    matchup: params.get("matchup") || "",
  };
}

function syncUrl() {
  const u = new URL(location.href);
  const setOrDel = (key, value, empty) => {
    if (!value || value === empty) u.searchParams.delete(key);
    else u.searchParams.set(key, value);
  };
  setOrDel("filter", state.filter, "all");
  setOrDel("class", state.classFilter, "all");
  setOrDel("view", state.view, "clips");
  setOrDel("source", state.source, "all");
  setOrDel("game", state.game, "all");
  setOrDel("event", state.event, "all");
  setOrDel("matchup", state.matchup.trim(), "");
  history.replaceState(null, "", u);
}

async function api(path, opts) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data.detail || res.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

function reviewApi(path) {
  if (!state.vodId) throw new Error("no VOD selected");
  const q = state.dayKey && state.dayKey !== "local"
    ? `?day=${encodeURIComponent(state.dayKey)}`
    : "";
  return `/api/review/${encodeURIComponent(state.vodId)}${path}${q}`;
}

function prettyDay(day) {
  const match = /^([a-z]{3})(\d{2})_(\d{4})$/i.exec(day || "");
  if (!match) return day || "";
  const months = {
    jan: "Jan", feb: "Feb", mar: "Mar", apr: "Apr", may: "May", jun: "Jun",
    jul: "Jul", aug: "Aug", sep: "Sep", oct: "Oct", nov: "Nov", dec: "Dec",
  };
  const month = months[match[1].toLowerCase()] || match[1];
  return `${month} ${Number(match[2])}, ${match[3]}`;
}

function reviewHref(dayKey, vodId) {
  if (dayKey && dayKey !== "local") {
    return `/review/${encodeURIComponent(dayKey)}/${encodeURIComponent(vodId)}`;
  }
  return `/review/${encodeURIComponent(vodId)}`;
}

function vodLabel(vod) {
  if (vod.localName) return vod.localName.replace(`${vod.dayKey}_`, "");
  return vod.vodId;
}

function openDayNav() {
  $("day-nav").classList.remove("hidden");
  $("btn-days").setAttribute("aria-expanded", "true");
}

function closeDayNav() {
  $("day-nav").classList.add("hidden");
  $("btn-days").setAttribute("aria-expanded", "false");
}

function renderDayNav() {
  const root = $("day-list");
  if (!root) return;
  root.innerHTML = "";
  const days = state.catalog?.days || [];
  if (!days.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.style.padding = "0.6rem 0.95rem";
    empty.textContent = "No days yet";
    root.appendChild(empty);
    return;
  }
  for (const day of days) {
    const group = document.createElement("div");
    group.className = "day-group";
    const label = document.createElement("div");
    label.className = "day-label";
    label.textContent = prettyDay(day.dayKey) || day.dayKey;
    group.appendChild(label);
    for (const vod of day.vods || []) {
      const btn = document.createElement("a");
      btn.className = "vod-btn" + (vod.vodId === state.vodId ? " on" : "");
      btn.href = reviewHref(vod.dayKey || day.dayKey, vod.vodId);
      const bits = [];
      if (vod.clipCount) bits.push(`${vod.clipCount} clips`);
      else if (vod.flags?.clips) bits.push("clips");
      if (vod.portraitCount) bits.push("9:16");
      btn.innerHTML = `<strong>${vodLabel(vod)}</strong><small>${bits.join(" · ") || (vod.local ? "local" : "gcs")}</small>`;
      group.appendChild(btn);
    }
    root.appendChild(group);
  }
}

async function loadCatalog() {
  try {
    state.catalog = await api("/api/catalog?gcs=false");
    renderDayNav();
  } catch (err) {
    console.warn(err);
  }
  try {
    const remote = await api("/api/catalog");
    state.catalog = remote;
    renderDayNav();
  } catch (err) {
    console.warn(err);
  }
}

function applyPayload(payload) {
  if (!payload || !payload.vodId) return;
  state.payload = payload;
  state.vodId = payload.vodId;
  state.dayKey = payload.dayKey;
  const title = payload.title || payload.vodId;
  document.title = `Review · ${prettyDay(payload.dayKey) || title}`;
  $("day-title").textContent = prettyDay(payload.dayKey) || "Clip review";
  $("source-line").textContent = [
    payload.vodId,
    payload.origin,
    `${payload.summary.total} clips`,
  ].filter(Boolean).join(" · ");
  $("btn-archive").href = "/";
  renderDayNav();
  if (payload.activeJob && payload.activeJob.running) showActiveJob(payload.activeJob);
  updateExportButtons();
}

function clipRating(clip) {
  return (state.payload?.selections?.[clip.id] || {}).rating || clip.rating || null;
}

function pickClips() {
  return (state.payload?.clips || []).filter((clip) => {
    const rating = clipRating(clip);
    return rating === "godly" || rating === "excellent";
  });
}

function pendingPickClips() {
  return pickClips().filter((clip) => !clip.local);
}

function isClipView() {
  return state.view === "clips" || state.view === "classifications";
}

function isClipItem(item) {
  return Boolean(item) && (!item.kind || item.kind === "clip");
}

function decorateViewItems() {
  const portraits = state.payload?.exports?.portraits || [];
  const decorated = state.payload?.exports?.decorated || [];
  const done = new Set(decorated.map((row) => row.weaveStem));
  const pending = portraits
    .filter((row) => !done.has(row.weaveStem))
    .map((row) => ({ ...row, exportStatus: "dry" }));
  const ready = decorated.map((row) => ({ ...row, exportStatus: "decorated" }));
  return [...pending, ...ready];
}

function musicViewItems() {
  const decorated = state.payload?.exports?.decorated || [];
  const music = state.payload?.exports?.music || [];
  const done = new Set(music.map((row) => row.weaveStem));
  const pending = decorated
    .filter((row) => !done.has(row.weaveStem))
    .map((row) => ({ ...row, exportStatus: "decorated" }));
  const ready = music.map((row) => ({ ...row, exportStatus: "music" }));
  return [...pending, ...ready];
}

function titlesViewItems() {
  return state.payload?.exports?.decorated || [];
}

function titleRecord(item) {
  const stem = item?.weaveStem;
  if (!stem) return null;
  return state.payload?.titles?.[stem] || null;
}

function titleIsGenerated(rec) {
  return Boolean(rec && ((rec.selected || "").trim() || (rec.suggestions || []).length));
}

function titleContextValue(item) {
  const stem = item?.weaveStem;
  if (!stem) return "";
  if (Object.prototype.hasOwnProperty.call(state.titleContext, stem)) {
    return state.titleContext[stem];
  }
  return titleRecord(item)?.userContext || "";
}

function setTitleContextValue(stem, value) {
  if (!stem) return;
  state.titleContext[stem] = value;
}

function titleRepromptValue(item) {
  const stem = item?.weaveStem;
  if (!stem) return "";
  return state.titleReprompt[stem] || "";
}

function setTitleRepromptValue(stem, value) {
  if (!stem) return;
  state.titleReprompt[stem] = value;
}

function pickedTitleHook(item, rec) {
  const stem = item?.weaveStem;
  const pick = stem ? state.titlePick[stem] : null;
  if (pick?.text) return pick;
  const selected = (rec?.selected || "").trim();
  const opt = titleHookOptions(rec).find((row) => row.text === selected);
  return opt || { text: selected, style: rec?.selectedStyle || "best" };
}

function setTitlePick(stem, text, style) {
  if (!stem) return;
  state.titlePick[stem] = { text, style: style || "best" };
  state.titleDraft[stem] = text;
}

function titleDraftValue(item, rec) {
  const stem = item?.weaveStem;
  if (stem && Object.prototype.hasOwnProperty.call(state.titleDraft, stem)) {
    return state.titleDraft[stem];
  }
  const pick = pickedTitleHook(item, rec);
  if (pick?.text) return pick.text;
  return (rec?.selected || "").trim();
}

function setTitleDraftValue(stem, value) {
  if (!stem) return;
  state.titleDraft[stem] = value;
}

function mountTitleDraftField(container, item, rec) {
  const stem = item?.weaveStem;
  const wrap = document.createElement("label");
  wrap.className = "title-context-wrap title-draft-wrap";
  const head = document.createElement("span");
  head.className = "title-context-label";
  head.textContent = "Upload title";
  wrap.appendChild(head);
  const input = document.createElement("textarea");
  input.className = "title-context-input title-draft-input";
  input.rows = 2;
  input.placeholder = "Pick a hook above or type your own";
  input.value = titleDraftValue(item, rec);
  input.addEventListener("click", (ev) => ev.stopPropagation());
  input.addEventListener("input", (ev) => {
    ev.stopPropagation();
    setTitleDraftValue(stem, ev.target.value);
    const approve = container.querySelector(".title-actions .primary");
    if (!approve) return;
    const draft = ev.target.value.trim();
    const approved = (rec?.selected || "").trim();
    const isSaved = (rec?.status === "approved" || rec?.status === "edited") && draft === approved;
    approve.disabled = !draft || isSaved;
    approve.textContent = isSaved ? "Saved ✓" : "Approve";
  });
  wrap.appendChild(input);
  container.appendChild(wrap);
  if (state.titleDraftFocus === item.id || state.titleDraftFocus === stem) {
    state.titleDraftFocus = "";
    requestAnimationFrame(() => {
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    });
  }
  return input;
}

function mountTitleRepromptField(container, item) {
  const stem = item?.weaveStem;
  const wrap = document.createElement("label");
  wrap.className = "title-context-wrap";
  const head = document.createElement("span");
  head.className = "title-context-label";
  head.textContent = "Refine (optional)";
  wrap.appendChild(head);
  const input = document.createElement("textarea");
  input.className = "title-context-input";
  input.rows = 2;
  input.placeholder = "e.g. less generic, lean into jungle experiment, more question hooks";
  input.value = titleRepromptValue(item);
  input.addEventListener("click", (ev) => ev.stopPropagation());
  input.addEventListener("input", (ev) => {
    ev.stopPropagation();
    setTitleRepromptValue(stem, ev.target.value);
  });
  wrap.appendChild(input);
  container.appendChild(wrap);
  return input;
}

function mountTitleContextField(container, item, { compact = false } = {}) {
  const stem = item?.weaveStem;
  const wrap = document.createElement("label");
  wrap.className = "title-context-wrap" + (compact ? " compact" : "");
  const head = document.createElement("span");
  head.className = "title-context-label";
  head.textContent = compact ? "Context" : "Optional context";
  wrap.appendChild(head);
  const input = document.createElement("textarea");
  input.className = "title-context-input";
  input.rows = compact ? 2 : 3;
  input.placeholder = "Main angle — hooks lean heavily into this (e.g. carrying Challenger players, jungle LB experiment)";
  input.value = titleContextValue(item);
  input.addEventListener("click", (ev) => ev.stopPropagation());
  input.addEventListener("input", (ev) => {
    ev.stopPropagation();
    setTitleContextValue(stem, ev.target.value);
  });
  wrap.appendChild(input);
  container.appendChild(wrap);
  return input;
}

function viewItems() {
  if (state.view === "stitched") return state.payload?.exports?.weaves || [];
  if (state.view === "portraits") return state.payload?.exports?.portraits || [];
  if (state.view === "decorate") return decorateViewItems();
  if (state.view === "music") return musicViewItems();
  if (state.view === "titles") return titlesViewItems();
  if (state.view === "post") return postViewItems();
  return filteredClips();
}

function missingWeaveGames() {
  const picks = state.payload?.exports?.pickGames || [];
  const have = new Set((state.payload?.exports?.weaves || []).map((row) => row.gameId).filter(Boolean));
  return picks.filter((id) => !have.has(id));
}

function missingPortraits() {
  const weaves = state.payload?.exports?.weaves || [];
  const have = new Set((state.payload?.exports?.portraits || []).map((row) => row.weaveStem));
  return weaves.filter((row) => !have.has(row.weaveStem));
}

function missingDecorated() {
  const portraits = state.payload?.exports?.portraits || [];
  const have = new Set((state.payload?.exports?.decorated || []).map((row) => row.weaveStem));
  return portraits.filter((row) => !have.has(row.weaveStem));
}

function missingMusic() {
  const decorated = state.payload?.exports?.decorated || [];
  const have = new Set((state.payload?.exports?.music || []).map((row) => row.weaveStem));
  return decorated.filter((row) => !have.has(row.weaveStem));
}

function poolTracks() {
  return (state.payload?.exports?.tracks || []).filter((track) => track.id && track.ready !== false);
}

function selectedTrackId() {
  const tracks = poolTracks();
  const wanted = state.musicTrack || $("track-pick")?.value || "";
  if (wanted && tracks.some((track) => track.id === wanted)) return wanted;
  return tracks[0]?.id || "";
}

function fillTrackSelect(select, current) {
  if (!select) return;
  const tracks = poolTracks();
  const chosen = current || selectedTrackId();
  select.innerHTML = "";
  if (!tracks.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "No pool tracks";
    select.appendChild(opt);
    select.disabled = true;
    return;
  }
  select.disabled = false;
  for (const track of tracks) {
    const opt = document.createElement("option");
    opt.value = track.id;
    const extra = (track.mood || []).slice(0, 2).join(" · ");
    opt.textContent = extra ? `${track.name} · ${extra}` : track.name;
    select.appendChild(opt);
  }
  if (chosen && tracks.some((track) => track.id === chosen)) select.value = chosen;
}

function setMusicTrack(trackId) {
  state.musicTrack = trackId || "";
  if (state.musicTrack) localStorage.setItem("reviewMusicTrack", state.musicTrack);
}

function trackPreviewUrl(trackId) {
  if (!trackId) return "";
  return `/api/music/preview?track=${encodeURIComponent(trackId)}`;
}

function syncTrackPreviewButtons() {
  const playingId = state.musicPreview.playing ? state.musicPreview.trackId : "";
  for (const btn of document.querySelectorAll(".btn-track-preview")) {
    let tid = btn.dataset.trackId || "";
    if (btn.id === "btn-track-preview") tid = $("track-pick")?.value || tid;
    else {
      const pick = btn.closest(".card-music-row")?.querySelector("select");
      if (pick) tid = pick.value || tid;
    }
    const playing = Boolean(playingId && playingId === tid);
    btn.classList.toggle("playing", playing);
    btn.textContent = playing ? "⏸" : "▶";
    btn.title = playing ? "Stop preview" : "Preview track";
    btn.setAttribute("aria-label", btn.title);
  }
}

function stopTrackPreview() {
  state.musicPreview.trackId = "";
  state.musicPreview.playing = false;
  const audio = $("track-preview");
  if (audio) {
    audio.pause();
    audio.removeAttribute("src");
  }
  syncTrackPreviewButtons();
}

async function toggleTrackPreview(trackId) {
  if (!trackId) return;
  const audio = $("track-preview");
  if (!audio) return;
  const same = state.musicPreview.trackId === trackId && state.musicPreview.playing;
  stopTrackPreview();
  if (same) return;
  audio.src = trackPreviewUrl(trackId);
  try {
    await audio.play();
    state.musicPreview.trackId = trackId;
    state.musicPreview.playing = true;
    syncTrackPreviewButtons();
  } catch (err) {
    stopTrackPreview();
    console.warn("track preview failed", err);
  }
}

function createTrackPreviewButton(getTrackId) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "ghost btn-track-preview";
  btn.textContent = "▶";
  btn.title = "Preview track";
  btn.setAttribute("aria-label", "Preview track");
  btn.addEventListener("click", (ev) => {
    ev.stopPropagation();
    const trackId = getTrackId();
    btn.dataset.trackId = trackId || "";
    toggleTrackPreview(trackId);
  });
  return btn;
}

function normalizeChampName(value) {
  return String(value || "").toLowerCase().replace(/[^a-z0-9]/g, "");
}

function clipOpponents(clip) {
  return [clip.opponentChampion, clip.laneOpponentChampion]
    .map(normalizeChampName)
    .filter(Boolean);
}

function parseMatchupQuery(raw) {
  const text = String(raw || "").trim();
  if (!text) return null;
  const parts = text
    .split(/\s+vs\.?\s+|\s+v\s+/i)
    .map(normalizeChampName)
    .filter(Boolean);
  if (parts.length >= 2) return { a: parts[0], b: parts[1] };
  const single = normalizeChampName(text.replace(/\s+/g, ""));
  return single ? { single } : null;
}

function opponentMatches(clip, needle) {
  if (!needle) return false;
  return clipOpponents(clip).some((opp) => opp.includes(needle));
}

function matchesMatchupSearch(clip, raw) {
  const parsed = parseMatchupQuery(raw);
  if (!parsed) return true;
  const champ = normalizeChampName(clip.champion);
  if (parsed.single) {
    return champ.includes(parsed.single) || opponentMatches(clip, parsed.single);
  }
  const { a, b } = parsed;
  return (
    (champ.includes(a) && opponentMatches(clip, b)) ||
    (champ.includes(b) && opponentMatches(clip, a))
  );
}

function filteredClips() {
  const clips = state.payload?.clips || [];
  return clips.filter((clip) => {
    const rating = clipRating(clip);
    if (state.filter === "unreviewed" && rating) return false;
    if (state.filter === "rejected" && rating !== "reject") return false;
    if (["keep", "excellent", "godly", "manual_edit"].includes(state.filter) && rating !== state.filter) {
      return false;
    }
    if (state.source === "local" && !clip.local) return false;
    if (state.source === "gcs" && clip.local) return false;
    if (state.game !== "all" && clip.gameId !== state.game) return false;
    if (state.event !== "all") {
      const needle = state.event.toUpperCase();
      if (!(clip.types || []).includes(needle)) return false;
    }
    if (state.classFilter === "unclassified" && clipIsClassified(clip)) return false;
    if (state.classFilter === "classified" && !clipIsClassified(clip)) return false;
    if (!matchesMatchupSearch(clip, state.matchup)) return false;
    return true;
  });
}

function currentClip() {
  const items = viewItems();
  if (!items.length) return null;
  state.index = Math.max(0, Math.min(state.index, items.length - 1));
  return items[state.index];
}

function renderSummary() {
  const s = state.payload?.summary || {};
  $("summary").innerHTML = [
    ["total", s.total, ""],
    ["local", s.local, "keep"],
    ["gcp", s.gcsOnly, ""],
    ["reviewed", s.reviewed, ""],
    ["unreviewed", s.unreviewed, ""],
    ["keep", s.keep, "keep"],
    ["excellent", s.excellent, "excellent"],
    ["godly", s.godly, "godly"],
    ["manual edit", s.manual_edit, "edit"],
    ["rejected", s.rejected, "reject"],
    ["classified", s.classified, ""],
    ["unclassified", s.unclassified, ""],
    ["titled", s.titled, ""],
    ["untitled", s.untitled, ""],
  ].map(([label, n, cls]) => `<span class="${cls}">${label} <strong>${n ?? 0}</strong></span>`).join("");
}

function renderSourceTabs() {
  const s = state.payload?.summary || {};
  renderChipGroup(
    $("source-tabs"),
    [
      { id: "all", label: "All", count: s.total ?? 0 },
      { id: "local", label: "Local", count: s.local ?? 0 },
      { id: "gcs", label: "GCP", count: s.gcsOnly ?? 0 },
    ],
    state.source,
    (id) => {
      state.source = id;
      state.index = 0;
      syncUrl();
      render();
    },
  );
}

function renderViewTabs() {
  const s = state.payload?.summary || {};
  const ex = state.payload?.exports || {};
  renderChipGroup(
    $("view-tabs"),
    [
      { id: "clips", label: "Clips", count: s.total ?? 0 },
      { id: "stitched", label: "Stitched", count: (ex.weaves || []).length },
      { id: "portraits", label: "Portraits", count: (ex.portraits || []).length },
      { id: "decorate", label: "Decorate", count: decorateViewItems().length },
      { id: "music", label: "Music", count: musicViewItems().length },
      { id: "titles", label: "Hooks", count: titlesViewItems().length },
      { id: "post", label: "Post", count: postViewItems().length },
      { id: "classifications", label: "Classes", count: s.classified ?? 0 },
    ],
    state.view,
    (id) => {
      if (state.playerOpen) closePlayer();
      if (id !== "music") stopTrackPreview();
      state.view = id;
      state.index = 0;
      syncUrl();
      render();
    },
  );
}

function renderChipGroup(root, items, current, onPick) {
  root.innerHTML = "";
  for (const item of items) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = item.id === current ? "on" : "";
    if (item.count != null) {
      btn.innerHTML = `${item.label}<small>${item.count}</small>`;
    } else {
      btn.textContent = item.label;
    }
    btn.addEventListener("click", () => onPick(item.id));
    root.appendChild(btn);
  }
}

function renderFilters() {
  const payload = state.payload || { games: [], events: [], summary: {} };
  renderChipGroup(
    $("filter-rating"),
    [
      { id: "all", label: "All" },
      { id: "unreviewed", label: "Unreviewed" },
      { id: "keep", label: "Keep" },
      { id: "excellent", label: "Excellent" },
      { id: "godly", label: "Godly" },
      { id: "manual_edit", label: "Manual Edit" },
      { id: "rejected", label: "Rejected" },
    ],
    state.filter,
    (id) => {
      state.filter = id;
      state.index = 0;
      syncUrl();
      render();
    },
  );
  renderChipGroup(
    $("filter-classification"),
    [
      { id: "all", label: "All classes" },
      { id: "unclassified", label: "Unclassified" },
      { id: "classified", label: "Classified" },
    ],
    state.classFilter,
    (id) => {
      state.classFilter = id;
      state.index = 0;
      syncUrl();
      render();
    },
  );
  renderChipGroup(
    $("filter-game"),
    [{ id: "all", label: "All games" }, ...payload.games.map((g) => ({ id: g, label: g }))],
    state.game,
    (id) => {
      state.game = id;
      state.index = 0;
      syncUrl();
      render();
    },
  );
  renderChipGroup(
    $("filter-event"),
    [{ id: "all", label: "All events" }, ...payload.events.map((e) => ({ id: e, label: e }))],
    state.event,
    (id) => {
      state.event = id;
      state.index = 0;
      syncUrl();
      render();
    },
  );
}

function clipClassification(clip) {
  const raw = clip?.classification || state.payload?.classifications?.[clip?.id] || null;
  if (!raw) return { rules: null, ai: null };
  if (raw.interpretation) {
    const source = raw.source || "rules";
    return {
      rules: source === "rules" ? raw : null,
      ai: source === "ai" ? raw : null,
    };
  }
  return {
    rules: raw.rules || null,
    ai: raw.ai || null,
  };
}

function reactionLabel(value) {
  if (!value) return "—";
  return String(value).charAt(0).toUpperCase() + String(value).slice(1);
}

function yesNo(value) {
  return value ? "Yes" : "No";
}

function classificationStatusLabel(status) {
  if (status === "approved") return "Approved";
  if (status === "edited") return "Edited";
  if (status === "pending") return "Pending review";
  return "";
}

function clipIsClassified(clip) {
  const bundle = clipClassification(clip);
  return Boolean(bundle.rules || bundle.ai);
}

function primaryLabel(interp) {
  return (interp?.primary || interp?.category || "ordinary").toUpperCase();
}

function renderSignalLine(signals) {
  if (!signals) return "—";
  const reaction = signals.reaction_level || signals.reaction;
  return [
    `Kills: ${signals.kills_in_10s ?? signals.kills ?? 0}`,
    `Reaction: ${reactionLabel(reaction)}`,
    `Low HP: ${yesNo(signals.low_hp)}`,
  ].join(" · ");
}

function renderClassificationCard(clip, source, rec, { title, mode, busy }) {
  const card = document.createElement("div");
  card.className = "classification-card";
  const head = document.createElement("p");
  head.className = "classification-card-head";
  head.textContent = title;
  card.appendChild(head);

  if (!rec) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = source === "ai" ? "ghost" : "primary";
    btn.textContent = busy ? "Classifying…" : source === "ai" ? "AI Classify" : "Classify";
    btn.disabled = busy;
    btn.addEventListener("click", () => classifyCurrentClip(mode));
    card.appendChild(btn);
    return card;
  }

  const interp = rec.interpretation || {};
  const hook = rec.hook || {};
  const category = document.createElement("p");
  category.className = "classification-category";
  category.textContent = primaryLabel(interp);
  card.appendChild(category);

  const confidence = document.createElement("p");
  confidence.className = "muted";
  confidence.textContent = `Confidence: ${interp.confidence ?? "—"}`;
  card.appendChild(confidence);

  if (interp.reason) {
    const reason = document.createElement("p");
    reason.className = "classification-reason muted";
    reason.textContent = interp.reason;
    card.appendChild(reason);
  }

  if ((interp.secondary || []).length) {
    const secondary = document.createElement("p");
    secondary.className = "muted";
    secondary.textContent = `Also: ${interp.secondary.join(", ")}`;
    card.appendChild(secondary);
  }

  const style = interp.hook_style || interp.hook_family;
  if (style) {
    const angle = document.createElement("p");
    angle.className = "classification-angle muted";
    angle.textContent = `Angle: ${style}`;
    card.appendChild(angle);
  }

  const hookLabel = document.createElement("p");
  hookLabel.className = "classification-hook-label";
  hookLabel.textContent = "Suggested hook:";
  card.appendChild(hookLabel);

  const editing = state.editingHook && state.editingHookSource === source;
  if (editing) {
    const input = document.createElement("input");
    input.type = "text";
    input.className = "hook-edit-input";
    input.value = hook.text || "";
    card.appendChild(input);
    const row = document.createElement("div");
    row.className = "classification-actions";
    const save = document.createElement("button");
    save.type = "button";
    save.className = "primary";
    save.textContent = "Save hook";
    save.addEventListener("click", () => {
      const text = input.value.trim();
      if (!text) return;
      saveClassificationHook(clip.id, text, source);
    });
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "ghost";
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", () => {
      state.editingHook = false;
      renderClassificationPanel(clip);
    });
    row.appendChild(save);
    row.appendChild(cancel);
    card.appendChild(row);
  } else {
    const hookText = document.createElement("p");
    hookText.className = "classification-hook";
    hookText.textContent = hook.text ? `"${hook.text}"` : "—";
    card.appendChild(hookText);

    const signalsEl = document.createElement("p");
    signalsEl.className = "classification-signals muted";
    signalsEl.textContent = renderSignalLine(interp.signals);
    card.appendChild(signalsEl);

    const status = document.createElement("p");
    status.className = "classification-status";
    status.textContent = classificationStatusLabel(rec.status);
    card.appendChild(status);

    const row = document.createElement("div");
    row.className = "classification-actions";
    const approve = document.createElement("button");
    approve.type = "button";
    approve.className = rec.status === "approved" ? "on" : "";
    approve.textContent = "Approve";
    approve.disabled = rec.status === "approved";
    approve.addEventListener("click", () => approveClassification(clip.id, source));
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = rec.status === "edited" ? "on" : "";
    edit.textContent = "Edit";
    edit.addEventListener("click", () => {
      state.editingHook = true;
      state.editingHookSource = source;
      renderClassificationPanel(clip);
      card.querySelector(".hook-edit-input")?.focus();
    });
    const rerun = document.createElement("button");
    rerun.type = "button";
    rerun.className = "ghost";
    rerun.textContent = busy ? "Classifying…" : "Re-run";
    rerun.disabled = busy;
    rerun.addEventListener("click", () => classifyCurrentClip(mode));
    row.appendChild(approve);
    row.appendChild(edit);
    row.appendChild(rerun);
    card.appendChild(row);
  }
  return card;
}

function renderClassificationPanel(clip) {
  const body = $("classification-body");
  if (!body) return;
  body.innerHTML = "";
  if (!clip) return;

  const bundle = clipClassification(clip);
  const grid = document.createElement("div");
  grid.className = "classification-grid";
  grid.appendChild(
    renderClassificationCard(clip, "rules", bundle.rules, {
      title: "Rules",
      mode: "rules",
      busy: state.classifying && state.classifyingMode === "rules",
    }),
  );
  grid.appendChild(
    renderClassificationCard(clip, "ai", bundle.ai, {
      title: "AI (metadata)",
      mode: "ai",
      busy: state.classifying && state.classifyingMode === "ai",
    }),
  );
  body.appendChild(grid);
}

function applyClassificationRecord(clipId, bundle) {
  if (!state.payload) return;
  state.payload.classifications = state.payload.classifications || {};
  state.payload.classifications[clipId] = bundle;
  const clip = (state.payload.clips || []).find((row) => row.id === clipId);
  if (clip) clip.classification = bundle;
  state.payload.summary = state.payload.summary || {};
  const classified = (state.payload.clips || []).filter((row) => clipIsClassified(row)).length;
  state.payload.summary.classified = classified;
  state.payload.summary.unclassified = (state.payload.clips || []).length - classified;
}

async function classifyCurrentClip(mode = "rules") {
  const clip = currentClip();
  if (!isClipItem(clip) || state.classifying) return;
  state.classifying = true;
  state.classifyingMode = mode;
  state.editingHook = false;
  renderClassificationPanel(clip);
  try {
    const bundle = await api(reviewApi("/classify"), {
      method: "POST",
      body: JSON.stringify({ id: clip.id, mode }),
    });
    applyClassificationRecord(clip.id, bundle);
    renderSummary();
    renderClassificationPanel(clip);
  } catch (err) {
    $("source-line").textContent = String(err.message || err);
    renderClassificationPanel(clip);
  } finally {
    state.classifying = false;
    state.classifyingMode = null;
  }
}

async function approveClassification(clipId, source = "rules") {
  try {
    const bundle = await api(reviewApi("/classifications"), {
      method: "PUT",
      body: JSON.stringify({ id: clipId, status: "approved", source }),
    });
    applyClassificationRecord(clipId, bundle);
    const clip = currentClip();
    if (clip) renderClassificationPanel(clip);
  } catch (err) {
    $("source-line").textContent = String(err.message || err);
  }
}

async function saveClassificationHook(clipId, hookText, source = "rules") {
  try {
    const bundle = await api(reviewApi("/classifications"), {
      method: "PUT",
      body: JSON.stringify({ id: clipId, status: "edited", hook_text: hookText, source }),
    });
    state.editingHook = false;
    applyClassificationRecord(clipId, bundle);
    const clip = currentClip();
    if (clip) renderClassificationPanel(clip);
  } catch (err) {
    $("source-line").textContent = String(err.message || err);
  }
}

function applyTitleRecord(weaveStem, record) {
  if (!state.payload || !weaveStem) return;
  state.payload.titles = state.payload.titles || {};
  state.payload.titles[weaveStem] = record;
  state.payload.summary = state.payload.summary || {};
  const decorated = state.payload.exports?.decorated || [];
  const titled = decorated.filter((row) => titleIsGenerated(state.payload.titles[row.weaveStem])).length;
  state.payload.summary.titled = titled;
  state.payload.summary.untitled = decorated.length - titled;
}

async function generateTitlesFor(exportId, { quiet = false, context, reprompt, previousHooks } = {}) {
  if (state.generatingTitles) return;
  const item = titlesViewItems().find((row) => row.id === exportId || row.weaveStem === exportId);
  const ctx = (context ?? titleContextValue(item)).trim();
  const refine = (reprompt ?? titleRepromptValue(item)).trim();
  const rec = titleRecord(item);
  const prev = previousHooks || (rec?.suggestions || []).slice(0, 5);
  state.generatingTitles = true;
  state.generatingTitleId = exportId;
  if (!quiet) {
    $("source-line").textContent = refine ? "Regenerating hooks with your notes…" : "Generating 5 hooks… usually 15–45s";
    renderTitlesView();
  }
  renderTitlePanel(currentClip());
  try {
    const body = { id: exportId };
    if (ctx) body.context = ctx;
    if (refine) body.reprompt = refine;
    if (prev.length) body.previous_hooks = prev;
    const record = await api(reviewApi("/titles/generate"), {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (item?.weaveStem) {
      applyTitleRecord(item.weaveStem, record);
      if (ctx) setTitleContextValue(item.weaveStem, ctx);
      if (refine) setTitleRepromptValue(item.weaveStem, refine);
      delete state.titlePick[item.weaveStem];
      delete state.titleDraft[item.weaveStem];
    }
    renderSummary();
    renderViewTabs();
    renderTitlesView();
    renderTitlePanel(currentClip());
  } catch (err) {
    if (!quiet) $("source-line").textContent = String(err.message || err);
  } finally {
    state.generatingTitles = false;
    state.generatingTitleId = "";
    renderTitlesView();
    renderTitlePanel(currentClip());
  }
}

function titleHookOptions(rec) {
  if (!rec) return [];
  const fromOpts = (rec.hookOptions || []).filter((row) => row?.text);
  if (fromOpts.length) return fromOpts;
  return (rec.suggestions || []).map((text, idx) => ({
    style: idx === 0 ? (rec.selectedStyle || "best") : "alt",
    text,
  }));
}

function hookStyleLabel(style) {
  const labels = {
    best: "best",
    curiosity: "curiosity",
    mistake: "mistake",
    educational: "educational",
    matchup: "matchup",
    challenge: "challenge",
    disbelief: "disbelief",
    cocky: "cocky",
    self_deprecating: "self-roast",
    outcome_tease: "tease",
    observation: "watch this",
    debate: "debate",
    ultra_short: "short",
    reaction: "reaction",
    alt: "alt",
  };
  return labels[style] || style || "hook";
}

function mountHookSuggestions(container, item, rec, { compact = false, max = 5 } = {}) {
  const selected = (titleDraftValue(item, rec) || pickedTitleHook(item, rec).text || "").trim();
  const options = titleHookOptions(rec).slice(0, max);
  const list = document.createElement("div");
  list.className = compact ? "suggestion-list" : "title-suggestions";
  if (!compact && options.length) {
    const hint = document.createElement("p");
    hint.className = "hook-pick-hint muted";
    hint.textContent = "Pick a hook · press 1–5 or click";
    container.appendChild(hint);
  }
  options.forEach((opt, idx) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = (compact ? "title-suggestion" : "title-suggestion-btn")
      + (opt.text === selected ? " on" : "");
    const key = document.createElement("span");
    key.className = "hook-key";
    key.textContent = idx < 5 ? String(idx + 1) : "·";
    const tag = document.createElement("span");
    tag.className = "hook-style-tag";
    tag.textContent = hookStyleLabel(opt.style);
    const text = document.createElement("span");
    text.className = "hook-text";
    text.textContent = opt.text;
    btn.append(key, tag, text);
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      setTitlePick(item.weaveStem, opt.text, opt.style);
      renderTitlePanel(item);
    });
    list.appendChild(btn);
  });
  container.appendChild(list);
  return list;
}

async function approveTitle(exportId, selected, selectedStyle) {
  try {
    const item = titlesViewItems().find((row) => row.id === exportId || row.weaveStem === exportId);
    const rec = titleRecord(item);
    const draft = (selected || titleDraftValue(item, rec)).trim();
    if (!draft) {
      $("source-line").textContent = "Pick a hook or type a custom title, then Approve";
      return;
    }
    const options = titleHookOptions(rec);
    const matched = options.find((row) => row.text === draft);
    const pick = selected
      ? { text: selected, style: selectedStyle || "best" }
      : matched || pickedTitleHook(item, rec);
    const isCustom = !matched;
    const body = {
      id: exportId,
      status: isCustom ? "edited" : "approved",
      selected: draft,
    };
    if (!isCustom && pick?.style) body.selected_style = pick.style;
    const record = await api(reviewApi("/titles"), {
      method: "PUT",
      body: JSON.stringify(body),
    });
    if (item?.weaveStem) {
      applyTitleRecord(item.weaveStem, record);
      setTitleDraftValue(item.weaveStem, record.selected || draft);
      delete state.titlePick[item.weaveStem];
    }
    const music = (state.payload?.exports?.music || []).find((row) => row.weaveStem === item?.weaveStem);
    if (music && record.selected) {
      await savePostMeta(music, { title: record.selected });
    }
    $("source-line").textContent = isCustom ? `Saved custom title: ${record.selected}` : `Approved: ${record.selected}`;
    renderSummary();
    renderViewTabs();
    renderTitlesView();
    renderTitlePanel(currentClip());
  } catch (err) {
    $("source-line").textContent = String(err.message || err);
  }
}

async function generateAllTitles() {
  const pending = titlesViewItems().filter((item) => !titleIsGenerated(titleRecord(item)));
  if (!pending.length || state.generatingTitles) return;
  for (const item of pending) {
    await generateTitlesFor(item.id, { quiet: true, context: titleContextValue(item).trim() || undefined });
  }
  $("source-line").textContent = `Generated hooks for ${pending.length} portrait${pending.length === 1 ? "" : "s"}.`;
  renderSummary();
  renderViewTabs();
  renderTitlesView();
}

function renderTitlePanel(item) {
  const panel = $("title-panel");
  const body = $("title-body");
  if (!panel || !body) return;
  const show = state.view === "titles" && item && !isClipItem(item);
  panel.classList.toggle("on", show);
  panel.classList.toggle("hidden", !show);
  body.innerHTML = "";
  if (!show) return;

  mountTitleContextField(body, item);
  mountTitleRepromptField(body, item);

  const rec = titleRecord(item);
  const busy = state.generatingTitles && (state.generatingTitleId === item.id || state.generatingTitleId === item.weaveStem);
  if (!rec || !(rec.suggestions || []).length) {
    if (busy) {
      const wait = document.createElement("p");
      wait.className = "hook-pick-hint muted";
      wait.textContent = "Generating 5 hooks… usually 15–45s";
      body.appendChild(wait);
    }
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "primary";
    btn.textContent = busy ? "Generating…" : "Generate 5 hooks";
    btn.disabled = busy;
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      generateTitlesFor(item.id);
    });
    body.appendChild(btn);
    return;
  }

  mountHookSuggestions(body, item, rec, { compact: false, max: 5 });

  if (rec.bestReason) {
    const reason = document.createElement("p");
    reason.className = "classification-reason muted";
    reason.textContent = `Why best: ${rec.bestReason}`;
    body.appendChild(reason);
  }

  if ((rec.hashtags || []).length) {
    const tags = document.createElement("p");
    tags.className = "muted title-hashtags";
    tags.textContent = rec.hashtags.slice(0, 4).map((tag) => `#${String(tag).replace(/^#/, "")}`).join(" ");
    body.appendChild(tags);
  }

  mountTitleDraftField(body, item, rec);

  const actions = document.createElement("div");
  actions.className = "title-actions";
  const regen = document.createElement("button");
  regen.type = "button";
  regen.className = "ghost";
  regen.textContent = busy ? "Generating…" : "Regenerate";
  regen.disabled = busy;
  regen.addEventListener("click", (ev) => {
    ev.stopPropagation();
    generateTitlesFor(item.id);
  });
  actions.appendChild(regen);
  const refine = document.createElement("button");
  refine.type = "button";
  refine.className = "ghost";
  refine.textContent = busy ? "…" : "Regenerate with notes";
  refine.disabled = busy;
  refine.addEventListener("click", (ev) => {
    ev.stopPropagation();
    generateTitlesFor(item.id, { reprompt: titleRepromptValue(item).trim() || undefined });
  });
  actions.appendChild(refine);
  const draft = titleDraftValue(item, rec).trim();
  if (draft) {
    const approved = (rec.selected || "").trim();
    const isSaved = (rec.status === "approved" || rec.status === "edited") && draft === approved;
    const approve = document.createElement("button");
    approve.type = "button";
    approve.className = "primary";
    approve.textContent = isSaved ? "Saved ✓" : "Approve";
    approve.disabled = isSaved;
    approve.addEventListener("click", (ev) => {
      ev.stopPropagation();
      approveTitle(item.id);
    });
    actions.appendChild(approve);
  }
  body.appendChild(actions);

  const status = document.createElement("p");
  status.className = "classification-status muted";
  const bits = [];
  if (rec.status) bits.push(`Status: ${rec.status}`);
  if (rec.selectedStyle) bits.push(`Style: ${hookStyleLabel(rec.selectedStyle)}`);
  status.textContent = bits.join(" · ");
  body.appendChild(status);
}

function renderTitlesView() {
  const root = $("titles-view");
  const grid = $("grid");
  const empty = $("empty");
  const items = titlesViewItems();
  const show = state.view === "titles";
  root.classList.toggle("hidden", !show);
  grid.classList.toggle("hidden", show);
  if (!show) return;
  root.innerHTML = "";
  empty.classList.toggle("hidden", items.length > 0);
  if (!items.length) {
    empty.textContent = "Decorate portraits first, then generate upload hooks here.";
    return;
  }

  const table = document.createElement("table");
  table.className = "classifications-table";
  table.innerHTML = `
    <thead>
      <tr>
        <th>Game</th>
        <th>Matchup</th>
        <th>Context</th>
        <th>Hooks</th>
        <th>Selected</th>
        <th>Tags</th>
        <th>Status</th>
        <th></th>
      </tr>
    </thead>
  `;
  const body = document.createElement("tbody");
  const current = currentClip();
  for (const item of items) {
    const rec = titleRecord(item);
    const selected = (rec?.selected || "").trim();
    const busy = state.generatingTitles && (state.generatingTitleId === item.id || state.generatingTitleId === item.weaveStem);
    const row = document.createElement("tr");
    if (current && current.id === item.id) row.classList.add("on");
    row.dataset.id = item.id;

    const matchup = [item.champion, item.opponentChampion ? `vs ${item.opponentChampion}` : null, item.result]
      .filter(Boolean)
      .join(" · ");

    const contextCell = document.createElement("td");
    contextCell.className = "title-context-col";
    mountTitleContextField(contextCell, item, { compact: true });

    const suggestionsCell = document.createElement("td");
    if ((rec?.suggestions || []).length || (rec?.hookOptions || []).length) {
      mountHookSuggestions(suggestionsCell, item, rec, { compact: true, max: 5 });
    } else {
      suggestionsCell.textContent = "—";
    }

    const tagsCell = document.createElement("td");
    tagsCell.className = "title-hashtags";
    tagsCell.textContent = (rec?.hashtags || []).length
      ? rec.hashtags.map((tag) => `#${String(tag).replace(/^#/, "")}`).join(" ")
      : "—";

    const actionCell = document.createElement("td");
    const genBtn = document.createElement("button");
    genBtn.type = "button";
    genBtn.className = rec?.suggestions?.length ? "ghost" : "primary";
    genBtn.textContent = busy ? "…" : rec?.suggestions?.length ? "Regen" : "Generate";
    genBtn.disabled = busy;
    genBtn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      generateTitlesFor(item.id);
    });
    actionCell.appendChild(genBtn);
    if ((rec?.suggestions || []).length) {
      const editBtn = document.createElement("button");
      editBtn.type = "button";
      editBtn.className = "ghost";
      editBtn.textContent = "Edit";
      editBtn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        state.titleDraftFocus = item.id;
        openPlayer(item.id, { playDay: false });
      });
      actionCell.appendChild(editBtn);
    }

    row.innerHTML = `
      <td><strong>${item.gameId || "game"}</strong><span class="muted">${item.id}</span></td>
      <td>${matchup || "—"}</td>
    `;
    row.appendChild(contextCell);
    row.appendChild(suggestionsCell);
    const selectedCell = document.createElement("td");
    selectedCell.textContent = selected || "—";
    row.appendChild(selectedCell);
    row.appendChild(tagsCell);
    const statusCell = document.createElement("td");
    statusCell.textContent = rec?.status || (rec?.suggestions?.length ? "pending" : "—");
    row.appendChild(statusCell);
    row.appendChild(actionCell);
    row.addEventListener("click", () => openPlayer(item.id, { playDay: false }));
    body.appendChild(row);
  }
  table.appendChild(body);
  root.appendChild(table);
}

function postViewItems() {
  return state.payload?.exports?.music || [];
}

function postRecord(item) {
  const rec = item?.post;
  return rec && typeof rec === "object" ? rec : null;
}

function postPlatformState(item, platform) {
  const rec = postRecord(item)?.[platform];
  return rec && typeof rec === "object" ? rec : {};
}

function postIsDone(item) {
  return Boolean(
    postPlatformState(item, "youtube").videoId || postPlatformState(item, "tiktok").publishId,
  );
}

function postingInfo(platform) {
  return state.payload?.posting?.[platform] || {};
}

function platformReady(platform) {
  const info = postingInfo(platform);
  if (!info.configured || !info.authorized) return false;
  // TikTok can be authorized while app review still withholds video.upload.
  return info.uploadScope !== false;
}

function chosenPlatforms() {
  return ["youtube", "tiktok"].filter((p) => state.postPlatforms[p] && platformReady(p));
}

function postTitleFor(item) {
  const fromSidecar = (postRecord(item)?.title || "").trim();
  if (fromSidecar) return fromSidecar;
  const fromHooks = (titleRecord(item)?.selected || "").trim();
  if (fromHooks) return fromHooks;
  const champ = item?.champion || "";
  const opp = item?.opponentChampion || "";
  const role = postRecord(item)?.role || "";
  const head = champ && opp ? `${champ} vs ${opp}` : champ || "League of Legends";
  return role ? `${head} ${role}` : head;
}

function slugHashtag(name) {
  return String(name || "")
    .toLowerCase()
    .replace(/[^a-z0-9]/g, "");
}

function postHashtagsFor(item, { platform = "instagram" } = {}) {
  const fromPost = (postRecord(item)?.hashtags || [])
    .map((tag) => String(tag).replace(/^#/, "").trim())
    .filter(Boolean);
  const fromHooks = (titleRecord(item)?.hashtags || [])
    .map((tag) => String(tag).replace(/^#/, "").trim())
    .filter(Boolean);
  const tags = [...(fromPost.length ? fromPost : fromHooks)];
  if (!tags.length) {
    for (const name of [item?.champion, item?.opponentChampion]) {
      const slug = slugHashtag(name);
      if (slug) tags.push(slug);
    }
    tags.push("leagueoflegends", "lolclips");
  }
  const extras =
    platform === "instagram"
      ? ["reels", "instagramreels", "lol"]
      : platform === "tiktok"
        ? ["fyp", "foryou", "lol"]
        : ["shorts"];
  for (const tag of extras) {
    if (!tags.includes(tag)) tags.push(tag);
  }
  // Drop YouTube-only shorts when copying for IG/TikTok.
  const filtered =
    platform === "instagram" || platform === "tiktok"
      ? tags.filter((tag) => tag !== "shorts")
      : tags;
  const seen = new Set();
  const out = [];
  for (const tag of filtered) {
    if (seen.has(tag)) continue;
    seen.add(tag);
    out.push(tag);
    if (out.length >= 10) break;
  }
  return out;
}

function postCaptionFor(item, platform = "instagram") {
  const title = postTitleFor(item);
  const tags = postHashtagsFor(item, { platform });
  const hashLine = tags.map((tag) => `#${tag}`).join(" ");
  return hashLine ? `${title}\n\n${hashLine}` : title;
}

async function copyPostCaption(item, platform, btn) {
  const title = postTitleFor(item);
  const text = postCaptionFor(item, platform);
  try {
    await navigator.clipboard.writeText(text);
    const label = platform === "instagram" ? "Instagram" : platform === "tiktok" ? "TikTok" : platform;
    $("source-line").textContent = `Copied ${label} caption (${title.slice(0, 40)}${title.length > 40 ? "…" : ""})`;
    if (btn) {
      btn.classList.add("copied");
      window.setTimeout(() => btn.classList.remove("copied"), 1200);
    }
  } catch (err) {
    $("source-line").textContent = `Copy failed: ${err.message || err}`;
  }
}

function platformIconSvg(platform) {
  if (platform === "instagram") {
    return `<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M7.5 2h9A5.5 5.5 0 0 1 22 7.5v9A5.5 5.5 0 0 1 16.5 22h-9A5.5 5.5 0 0 1 2 16.5v-9A5.5 5.5 0 0 1 7.5 2zm0 1.8A3.7 3.7 0 0 0 3.8 7.5v9a3.7 3.7 0 0 0 3.7 3.7h9a3.7 3.7 0 0 0 3.7-3.7v-9a3.7 3.7 0 0 0-3.7-3.7h-9zm9.75 1.45a1.15 1.15 0 1 1 0 2.3 1.15 1.15 0 0 1 0-2.3zM12 7.2A4.8 4.8 0 1 1 12 16.8 4.8 4.8 0 0 1 12 7.2zm0 1.8a3 3 0 1 0 0 6 3 3 0 0 0 0-6z"/></svg>`;
  }
  if (platform === "tiktok") {
    return `<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M14.2 3h2.1c.2 1.7 1.2 3.2 2.7 4.1 1 .6 2.1.9 3.2.9v2.2c-1.5 0-2.9-.4-4.2-1.1v6.5c0 3.4-2.8 6.2-6.2 6.2S5.6 18.9 5.6 15.5 8.4 9.3 11.8 9.3c.3 0 .7 0 1 .1v2.3c-.3-.1-.6-.1-1-.1-2.2 0-3.9 1.8-3.9 3.9s1.8 3.9 3.9 3.9 3.9-1.8 3.9-3.9V3z"/></svg>`;
  }
  return "";
}

function copyCaptionButtons(item) {
  const wrap = document.createElement("div");
  wrap.className = "post-copy-btns";
  for (const platform of ["instagram", "tiktok"]) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `post-copy-btn post-copy-${platform}`;
    btn.title =
      platform === "instagram"
        ? "Copy title + hashtags for Instagram Reels"
        : "Copy title + hashtags for TikTok";
    btn.setAttribute("aria-label", btn.title);
    btn.innerHTML = platformIconSvg(platform);
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      copyPostCaption(item, platform, btn);
    });
    wrap.appendChild(btn);
  }
  return wrap;
}

function applyPostRecord(weaveStem, record) {
  const rows = state.payload?.exports?.music || [];
  for (const row of rows) {
    if (row.weaveStem === weaveStem) row.post = record;
  }
  if (state.payload?.summary) {
    const posted = rows.filter((row) => postIsDone(row)).length;
    state.payload.summary.posted = posted;
    state.payload.summary.unposted = rows.length - posted;
  }
}

async function savePostMeta(item, { title } = {}) {
  try {
    const body = { id: item.id };
    if (title != null) body.title = title;
    const record = await api(reviewApi("/post/meta"), {
      method: "PUT",
      body: JSON.stringify(body),
    });
    applyPostRecord(item.weaveStem, record);
    state.editingPostId = "";
    renderPostView();
    renderViewTabs();
    updateExportButtons();
    return record;
  } catch (err) {
    $("source-line").textContent = String(err.message || err);
    return null;
  }
}

async function syncPostTitles() {
  const items = postViewItems();
  if (!items.length) return;
  for (const item of items) {
    await savePostMeta(item);
  }
  $("source-line").textContent = `Refreshed titles for ${items.length} short${items.length === 1 ? "" : "s"}.`;
}

function platformFlags() {
  return chosenPlatforms().map((p) => `--${p}`);
}

function postOne(item) {
  const flags = platformFlags();
  const only = item?.gameId || item?.weaveStem;
  if (!flags.length || !only || state.jobRunning) return;
  const extra = [...flags, "--only", only];
  if (postIsDone(item)) extra.push("--force");
  runReviewJob("review-post", extra, { mixTargetId: item.id });
}

function postPending() {
  const flags = platformFlags();
  if (!flags.length || state.jobRunning) return;
  runReviewJob("review-post", flags, { mixTargetId: "" });
}

function postCell(item, platform) {
  const cell = document.createElement("td");
  const rec = postPlatformState(item, platform);
  if (platform === "youtube" && rec.url) {
    const link = document.createElement("a");
    link.href = rec.studioUrl || rec.url;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = rec.privacy || "uploaded";
    link.addEventListener("click", (ev) => ev.stopPropagation());
    cell.appendChild(link);
    return cell;
  }
  if (platform === "tiktok" && rec.publishId) {
    cell.textContent = rec.status === "SEND_TO_USER_INBOX" ? "in inbox" : rec.status || "draft";
    return cell;
  }
  if (rec.status === "failed") {
    cell.className = "post-failed";
    cell.textContent = rec.error ? String(rec.error).slice(0, 60) : "failed";
    cell.title = rec.error || "";
    return cell;
  }
  cell.className = "muted";
  cell.textContent = platformReady(platform) ? "—" : "not set up";
  return cell;
}

function renderPostView() {
  const root = $("post-view");
  const grid = $("grid");
  const empty = $("empty");
  if (!root) return;
  const items = postViewItems();
  const show = state.view === "post";
  root.classList.toggle("hidden", !show);
  if (!show) return;
  grid.classList.add("hidden");
  root.innerHTML = "";
  empty.classList.toggle("hidden", items.length > 0);
  if (!items.length) {
    empty.textContent = "Add music first — the mix in post/ is what gets uploaded.";
    return;
  }

  const table = document.createElement("table");
  table.className = "classifications-table";
  table.innerHTML = `
    <thead>
      <tr>
        <th>Game</th>
        <th>Matchup</th>
        <th>Title</th>
        <th>Copy</th>
        <th>Track</th>
        <th>YouTube</th>
        <th>TikTok</th>
        <th></th>
      </tr>
    </thead>
  `;
  const body = document.createElement("tbody");
  const current = currentClip();
  for (const item of items) {
    const row = document.createElement("tr");
    if (current && current.id === item.id) row.classList.add("on");
    row.dataset.id = item.id;

    const matchup = [item.champion, item.opponentChampion ? `vs ${item.opponentChampion}` : null, item.result]
      .filter(Boolean)
      .join(" · ");

    row.innerHTML = `
      <td><strong>${item.gameId || "game"}</strong><span class="muted">${postRecord(item)?.role || ""}</span></td>
      <td>${matchup || "—"}</td>
    `;

    const titleCell = document.createElement("td");
    titleCell.className = "post-title-col";
    if (state.editingPostId === item.id) {
      const input = document.createElement("input");
      input.type = "text";
      input.className = "hook-edit-input";
      input.value = postTitleFor(item);
      input.maxLength = 100;
      input.addEventListener("click", (ev) => ev.stopPropagation());
      input.addEventListener("keydown", (ev) => {
        ev.stopPropagation();
        if (ev.key === "Enter") savePostMeta(item, { title: input.value.trim() });
        if (ev.key === "Escape") {
          state.editingPostId = "";
          renderPostView();
        }
      });
      titleCell.appendChild(input);
      const save = document.createElement("button");
      save.type = "button";
      save.className = "primary";
      save.textContent = "Save";
      save.addEventListener("click", (ev) => {
        ev.stopPropagation();
        savePostMeta(item, { title: input.value.trim() });
      });
      titleCell.appendChild(save);
    } else {
      const text = document.createElement("button");
      text.type = "button";
      text.className = "post-title-btn";
      text.textContent = postTitleFor(item);
      text.title = "Click to edit the upload title";
      text.addEventListener("click", (ev) => {
        ev.stopPropagation();
        state.editingPostId = item.id;
        renderPostView();
      });
      titleCell.appendChild(text);
      const tags = postHashtagsFor(item, { platform: "instagram" });
      if (tags.length) {
        const tagLine = document.createElement("div");
        tagLine.className = "muted title-hashtags post-hashtag-preview";
        tagLine.textContent = tags
          .slice(0, 5)
          .map((tag) => `#${tag}`)
          .join(" ");
        titleCell.appendChild(tagLine);
      }
    }
    row.appendChild(titleCell);

    const copyCell = document.createElement("td");
    copyCell.className = "post-copy-col";
    copyCell.appendChild(copyCaptionButtons(item));
    row.appendChild(copyCell);

    const trackCell = document.createElement("td");
    trackCell.className = "muted";
    trackCell.textContent = item.trackName || item.trackId || "—";
    row.appendChild(trackCell);

    row.appendChild(postCell(item, "youtube"));
    row.appendChild(postCell(item, "tiktok"));

    const actionCell = document.createElement("td");
    const btn = document.createElement("button");
    btn.type = "button";
    const busy = Boolean(state.jobRunning);
    const mine = state.mixTargetId === item.id;
    btn.className = postIsDone(item) ? "ghost" : "primary";
    if (mine && busy) btn.textContent = "Posting…";
    else if (busy) btn.textContent = "…";
    else btn.textContent = postIsDone(item) ? "Repost" : "Post";
    btn.disabled = busy || !chosenPlatforms().length;
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      postOne(item);
    });
    actionCell.appendChild(btn);
    row.appendChild(actionCell);

    row.addEventListener("click", () => openPlayer(item.id, { playDay: false }));
    body.appendChild(row);
  }
  table.appendChild(body);
  root.appendChild(table);
}

function ratingBadge(rating) {
  return rating ? (RATING_LABEL[rating] || rating) : "unreviewed";
}

const thumbObserver = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    const el = entry.target;
    const url = el.dataset.thumb;
    if (url) {
      el.style.backgroundImage = `url("${url}")`;
      delete el.dataset.thumb;
    }
    thumbObserver.unobserve(el);
  }
}, { rootMargin: "240px" });

const previewObserver = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    const video = entry.target;
    const url = video.dataset.preview;
    if (!url) continue;
    if (entry.isIntersecting) {
      const href = new URL(url, location.origin).href;
      if (video.src !== href) video.src = url;
      video.play().catch(() => {});
    } else {
      video.pause();
    }
  }
}, { rootMargin: "120px" });

function pauseGridPreviews() {
  for (const video of $("grid").querySelectorAll("video")) video.pause();
}

function highlightCurrent() {
  const current = currentClip();
  for (const card of $("grid").querySelectorAll(".card")) {
    card.classList.toggle("on", Boolean(current) && card.dataset.id === current.id);
  }
  for (const row of $("classifications-view").querySelectorAll("tbody tr")) {
    row.classList.toggle("on", Boolean(current) && row.dataset.id === current.id);
  }
  for (const row of $("titles-view").querySelectorAll("tbody tr")) {
    row.classList.toggle("on", Boolean(current) && row.dataset.id === current.id);
  }
  for (const row of $("post-view").querySelectorAll("tbody tr")) {
    row.classList.toggle("on", Boolean(current) && row.dataset.id === current.id);
  }
}

function classificationCell(clip, source) {
  const rec = clipClassification(clip)[source];
  if (!rec) return "—";
  const interp = rec.interpretation || {};
  const conf = interp.confidence != null ? ` ${Math.round(interp.confidence * 100)}%` : "";
  return `${primaryLabel(interp)}${conf}`;
}

function classificationHook(clip, source) {
  const rec = clipClassification(clip)[source];
  const text = rec?.hook?.text;
  return text ? `"${text}"` : "—";
}

function classificationAngle(clip, source) {
  const rec = clipClassification(clip)[source];
  const interp = rec?.interpretation || {};
  return interp.hook_style || interp.hook_family || "—";
}

function renderClassificationsView() {
  const root = $("classifications-view");
  const grid = $("grid");
  const empty = $("empty");
  const items = filteredClips();
  const show = state.view === "classifications";
  root.classList.toggle("hidden", !show);
  grid.classList.toggle("hidden", show);
  if (!show) return;
  root.innerHTML = "";
  empty.classList.toggle("hidden", items.length > 0);
  if (!items.length) return;

  const table = document.createElement("table");
  table.className = "classifications-table";
  table.innerHTML = `
    <thead>
      <tr>
        <th>Clip</th>
        <th>Event</th>
        <th>Rules</th>
        <th>AI</th>
        <th>Angle</th>
        <th>Hook</th>
        <th>Status</th>
      </tr>
    </thead>
  `;
  const body = document.createElement("tbody");
  const current = currentClip();
  for (const clip of items) {
    const bundle = clipClassification(clip);
    const status = [bundle.rules?.status, bundle.ai?.status].filter(Boolean).join(" · ") || "—";
    const row = document.createElement("tr");
    if (current && current.id === clip.id) row.classList.add("on");
    row.dataset.id = clip.id;
    row.innerHTML = `
      <td><strong>${clip.id}</strong><span class="muted">${clip.gameId} · ${clip.champion}</span></td>
      <td>${clip.event || "—"}</td>
      <td>${classificationCell(clip, "rules")}</td>
      <td>${classificationCell(clip, "ai")}</td>
      <td>${classificationAngle(clip, "ai") !== "—" ? classificationAngle(clip, "ai") : classificationAngle(clip, "rules")}</td>
      <td class="hook-col">${classificationHook(clip, "rules") !== "—" ? classificationHook(clip, "rules") : classificationHook(clip, "ai")}</td>
      <td>${status}</td>
    `;
    row.addEventListener("click", () => openPlayer(clip.id, { playDay: false }));
    body.appendChild(row);
  }
  table.appendChild(body);
  root.appendChild(table);
}

function emptyMessage() {
  if (state.view === "stitched") {
    const n = (state.payload?.exports?.pickGames || []).length;
    if (!n) return "Rate godly / excellent clips first. Those games stitch here.";
    const missing = missingWeaveGames();
    if (missing.length) return `${missing.length} of ${n} pick games are not stitched yet.`;
    return "No stitched games.";
  }
  if (state.view === "portraits") {
    if (!(state.payload?.exports?.weaves || []).length) return "Stitch games first, then make portraits.";
    return "No dry portraits yet. Make portraits from the stitched games.";
  }
  if (state.view === "decorate") return "No portraits to decorate yet.";
  if (state.view === "music") {
    if (!(state.payload?.exports?.decorated || []).length) return "Decorate first, then add music.";
    return "No decorated portraits to mix yet.";
  }
  if (state.view === "titles") {
    if (!(state.payload?.exports?.decorated || []).length) return "Decorate portraits first, then generate upload hooks.";
    return "No decorated portraits yet.";
  }
  return "No clips match this filter.";
}

function renderGrid() {
  const items = viewItems();
  const grid = $("grid");
  grid.innerHTML = "";
  const portraitish = state.view === "portraits" || state.view === "decorate" || state.view === "music";
  grid.classList.toggle("portrait-grid", portraitish);
  $("empty").textContent = emptyMessage();
  $("empty").classList.toggle("hidden", items.length > 0 || state.view === "classifications" || state.view === "titles");
  if (state.view === "classifications" || state.view === "titles") return;
  const current = currentClip();
  for (const item of items) {
    if (isClipItem(item)) renderClipCard(item, current);
    else renderExportCard(item, current);
  }
  if (state.view === "music") syncTrackPreviewButtons();
}

function renderClipCard(clip, current) {
  const rating = clipRating(clip);
  const card = document.createElement("div");
  card.className = "card" + (current && current.id === clip.id ? " on" : "");
  card.tabIndex = 0;
  card.dataset.id = clip.id;
  if (rating) card.dataset.rating = rating;
  card.dataset.local = clip.local ? "true" : "false";
  const thumb = document.createElement("div");
  thumb.className = "thumb";
  if (clip.previewUrl) {
    const video = document.createElement("video");
    video.muted = true;
    video.loop = true;
    video.playsInline = true;
    video.preload = "none";
    video.dataset.preview = clip.previewUrl;
    video.addEventListener("error", () => {
      previewObserver.unobserve(video);
      video.remove();
      if (clip.thumbUrl) {
        thumb.dataset.thumb = clip.thumbUrl;
        thumbObserver.observe(thumb);
      }
    });
    thumb.appendChild(video);
    previewObserver.observe(video);
  } else if (clip.thumbUrl) {
    thumb.dataset.thumb = clip.thumbUrl;
    thumbObserver.observe(thumb);
  }
  const place = document.createElement("span");
  place.className = "place";
  place.textContent = clip.local ? "local" : "gcp";
  const badge = document.createElement("span");
  badge.className = "badge";
  badge.textContent = ratingBadge(rating);
  const classBadge = document.createElement("span");
  classBadge.className = "class-badge";
  const bundle = clipClassification(clip);
  const rulesLabel = bundle.rules ? primaryLabel(bundle.rules.interpretation) : null;
  const aiLabel = bundle.ai ? primaryLabel(bundle.ai.interpretation) : null;
  if (rulesLabel && aiLabel && rulesLabel !== aiLabel) {
    classBadge.textContent = `${rulesLabel} / ${aiLabel}`;
  } else if (rulesLabel || aiLabel) {
    classBadge.textContent = rulesLabel || aiLabel;
  } else {
    classBadge.textContent = "unclassified";
    classBadge.classList.add("muted");
  }
  const cap = document.createElement("figcaption");
  cap.innerHTML = `<strong>${clip.gameId} · ${clip.champion}</strong>${clip.event} · ${clip.gameTime || "—"}`;
  card.appendChild(thumb);
  card.appendChild(place);
  card.appendChild(badge);
  card.appendChild(classBadge);
  card.appendChild(cap);
  card.addEventListener("click", () => openPlayer(clip.id, { playDay: false }));
  $("grid").appendChild(card);
}

function renderExportCard(item, current) {
  const card = document.createElement("div");
  card.className = "card" + (current && current.id === item.id ? " on" : "");
  card.tabIndex = 0;
  card.dataset.id = item.id;
  card.dataset.kind = item.kind || "";
  if (item.exportStatus) card.dataset.status = item.exportStatus;
  card.dataset.local = item.local ? "true" : "false";
  const thumb = document.createElement("div");
  thumb.className = "thumb";
  if (item.thumbUrl) {
    thumb.dataset.thumb = item.thumbUrl;
    thumbObserver.observe(thumb);
  }
  const place = document.createElement("span");
  place.className = "place";
  place.textContent = item.kind === "weave" ? "stitch" : item.kind;
  const badge = document.createElement("span");
  badge.className = "badge";
  if (item.exportStatus === "dry") {
    badge.dataset.status = "dry";
    badge.textContent = "not decorated";
  } else if (item.exportStatus === "music") {
    badge.dataset.status = "music";
    badge.textContent = item.trackName || item.trackId || "music";
  } else if (item.exportStatus === "decorated") {
    badge.dataset.status = "decorated";
    badge.textContent = state.view === "music" ? "no music" : "decorated";
  } else if (item.clipCount != null) {
    badge.textContent = `${item.clipCount} clips`;
  } else {
    badge.textContent = item.kind || "";
  }
  const cap = document.createElement("figcaption");
  const vs = item.opponentChampion ? `vs ${item.opponentChampion}` : (item.result || item.kind);
  cap.innerHTML = `<strong>${item.gameId || "game"} · ${item.champion || item.title}</strong>${vs}`;
  card.appendChild(thumb);
  card.appendChild(place);
  card.appendChild(badge);
  card.appendChild(cap);
  if (state.view === "music") {
    const row = document.createElement("div");
    row.className = "card-music-row";
    const pick = document.createElement("select");
    pick.setAttribute("aria-label", "Track for this game");
    fillTrackSelect(pick, item.trackId || selectedTrackId());
    pick.addEventListener("click", (ev) => ev.stopPropagation());
    pick.addEventListener("change", () => {
      if (state.musicPreview.playing && state.musicPreview.trackId !== pick.value) stopTrackPreview();
    });
    const preview = createTrackPreviewButton(() => pick.value);
    const mix = document.createElement("button");
    mix.type = "button";
    const busy = Boolean(state.jobRunning);
    const mine = state.mixTargetId === item.id;
    if (mine && state.mixStatus === "done") {
      mix.textContent = "Done";
      mix.className = "primary";
    } else if (mine && busy) {
      mix.textContent = "Mixing…";
    } else if (busy) {
      mix.textContent = "…";
    } else {
      mix.textContent = item.exportStatus === "music" ? "Remix" : "Mix";
    }
    mix.disabled = !pick.value || busy;
    mix.addEventListener("click", (ev) => {
      ev.stopPropagation();
      mixOne(item, pick.value);
    });
    row.appendChild(pick);
    row.appendChild(preview);
    row.appendChild(mix);
    card.appendChild(row);
  }
  card.addEventListener("click", () => openPlayer(item.id, { playDay: false }));
  $("grid").appendChild(card);
}

function preloadNext() {
  const items = viewItems();
  const next = items[state.index + 1];
  const el = $("preload");
  if (!next) {
    el.removeAttribute("src");
    return;
  }
  if (el.src !== new URL(next.mediaUrl, location.origin).href) el.src = next.mediaUrl;
}

function updateRevealButton(item) {
  const btn = $("btn-reveal-local");
  if (!btn) return;
  const show = Boolean(item?.local && item?.relativePath);
  btn.classList.toggle("hidden", !show);
  btn.textContent = REVEAL_LABEL;
  btn.disabled = false;
}

async function revealCurrentClip() {
  const item = currentClip();
  if (!item?.local || !item?.relativePath) return;
  const btn = $("btn-reveal-local");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Opening…";
  }
  try {
    const result = await api(reviewApi("/reveal"), {
      method: "POST",
      body: JSON.stringify({ id: item.id }),
    });
    if (btn) btn.textContent = result.explorer ? `Opened in ${result.explorer}` : "Opened";
    setTimeout(() => updateRevealButton(currentClip()), 1200);
  } catch (err) {
    const msg = String(err.message || err);
    if (btn) btn.textContent = "Failed — retry";
    $("source-line").textContent = msg;
    setTimeout(() => updateRevealButton(currentClip()), 2000);
  }
}

function showClip(clip, { autoplay = true } = {}) {
  const player = $("player");
  const clipMode = isClipItem(clip);
  const same = player.getAttribute("data-clip") === clip.id;
  player.setAttribute("data-clip", clip.id);
  $("player-mode").classList.toggle("export-player", !clipMode);
  const frame = player.closest(".player-frame");
  if (frame) frame.dataset.kind = clip.kind || "clip";
  if (!same) {
    player.src = clip.mediaUrl;
    state.trimIn = null;
    state.trimOut = null;
  }
  player.playbackRate = state.speed;
  if (autoplay) player.play().catch(() => {});
  const items = viewItems();
  const label = clipMode
    ? "Clip"
    : clip.kind === "weave"
      ? "Stitch"
      : clip.kind === "music"
        ? "Music"
        : clip.kind === "decorated"
          ? "Decorated"
          : "Portrait";
  $("player-index").textContent = `${label} ${state.index + 1} / ${items.length}`;
  if (clipMode) {
    $("player-title").textContent = `${clip.gameId} · ${clip.champion}`;
    $("player-sub").textContent = [clip.event, clip.gameTime, clip.opponentChampion ? `vs ${clip.opponentChampion}` : null]
      .filter(Boolean)
      .join(" · ");
    $("player-place").textContent = clip.local
      ? (clip.trimmed ? "On disk · trimmed" : "On disk")
      : "GCP only";
  } else {
    $("player-title").textContent = clip.title || clip.filename || clip.id;
    $("player-sub").textContent = [
      clip.gameId,
      clip.opponentChampion ? `vs ${clip.opponentChampion}` : null,
      clip.clipCount != null ? `${clip.clipCount} clips` : null,
    ].filter(Boolean).join(" · ");
    $("player-place").textContent =
      clip.exportStatus === "music" || clip.kind === "music"
        ? clip.trackName
          ? `On disk · ${clip.trackName}`
          : "On disk · music"
        : clip.exportStatus === "decorated" || clip.kind === "decorated"
          ? state.view === "music"
            ? "On disk · decorated · no music yet"
            : "On disk · decorated"
          : clip.kind === "portrait"
            ? "On disk · dry portrait"
            : "On disk · stitched";
  }
  updatePlayerTime();
  updateTrimUI();
  const rating = clipRating(clip);
  $("player-rating").textContent = ratingBadge(rating);
  for (const btn of $("rate-row").querySelectorAll("button[data-rating]")) {
    const value = btn.dataset.rating;
    btn.classList.toggle("on", value === rating);
  }
  preloadNext();
  highlightCurrent();
  state.editingHook = false;
  updateRevealButton(clip);
  if (clipMode) renderClassificationPanel(clip);
  else renderTitlePanel(clip);
}

function openPlayer(clipId, { playDay }) {
  stopTrackPreview();
  pauseGridPreviews();
  const items = viewItems();
  const idx = items.findIndex((c) => c.id === clipId);
  if (idx >= 0) state.index = idx;
  const clip = currentClip();
  if (!clip) return;
  state.playerOpen = true;
  state.playDay = Boolean(playDay);
  $("player-mode").classList.remove("hidden");
  $("btn-play-day").textContent = state.playDay ? "Exit Play Day" : "Play Day";
  showClip(clip, { autoplay: true });
}

function closePlayer() {
  state.playerOpen = false;
  state.playDay = false;
  $("player-mode").classList.add("hidden");
  $("player-mode").classList.remove("export-player");
  $("btn-play-day").textContent = "Play Day";
  const player = $("player");
  player.pause();
  highlightCurrent();
}

function step(delta) {
  const items = viewItems();
  if (!items.length) return;
  state.index = (state.index + delta + items.length) % items.length;
  const clip = currentClip();
  if (state.playerOpen) showClip(clip, { autoplay: true });
  else renderGrid();
}

function setRating(rating) {
  const clip = currentClip();
  if (!isClipItem(clip)) return;
  const ratedId = clip.id;
  const bodyRating = rating === "clear" ? null : rating;
  state.saving = state.saving.then(async () => {
    const payload = await api(reviewApi("/selections"), {
      method: "PUT",
      body: JSON.stringify({ id: clip.id, rating: bodyRating }),
    });
    applyPayload(payload);
    renderSummary();
    renderSourceTabs();
    renderFilters();
    updateExportButtons();
    const items = viewItems();
    const still = items.findIndex((c) => c.id === ratedId);
    if (still >= 0) state.index = still;
    else if (state.index >= items.length) state.index = Math.max(0, items.length - 1);

    const shouldAdvance = state.autoAdvance && rating && rating !== "clear";
    if (shouldAdvance && still >= 0 && still < items.length - 1) {
      step(1);
      return;
    }
    if (shouldAdvance && still < 0 && items.length && state.playerOpen) {
      showClip(currentClip(), { autoplay: true });
      return;
    }
    if (state.playerOpen) {
      const now = currentClip();
      if (!now) closePlayer();
      else {
        $("player-rating").textContent = ratingBadge(clipRating(now));
        for (const btn of $("rate-row").querySelectorAll("button[data-rating]")) {
          btn.classList.toggle("on", btn.dataset.rating === clipRating(now));
        }
        highlightCurrent();
      }
    } else {
      renderGrid();
    }
  }).catch((err) => {
    $("source-line").textContent = String(err.message || err);
  });
}

function togglePlay() {
  const clip = currentClip();
  if (!clip) return;
  if (!state.playerOpen) {
    openPlayer(clip.id, { playDay: false });
    return;
  }
  const player = $("player");
  if (player.paused) player.play().catch(() => {});
  else player.pause();
}

function toggleFullscreen() {
  const frame = document.querySelector(".player-frame");
  if (!document.fullscreenElement) frame.requestFullscreen?.().catch(() => {});
  else document.exitFullscreen?.().catch(() => {});
}

function seekBy(seconds) {
  if (!state.playerOpen) return;
  const player = $("player");
  const now = Number.isFinite(player.currentTime) ? player.currentTime : 0;
  const duration = Number.isFinite(player.duration) ? player.duration : now + Math.abs(seconds);
  player.currentTime = Math.min(Math.max(0, now + seconds), Math.max(0, duration));
}

function fmtTime(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const m = Math.floor(Math.max(0, seconds) / 60);
  const s = Math.max(0, seconds) - m * 60;
  return `${m}:${s.toFixed(1).padStart(4, "0")}`;
}

function updatePlayerTime() {
  const player = $("player");
  const el = $("player-time");
  if (!el) return;
  el.textContent = `${fmtTime(player.currentTime)} / ${fmtTime(player.duration)}`;
}

function trimRange() {
  let start = state.trimIn;
  let end = state.trimOut;
  if (start == null || end == null) return null;
  if (end < start) [start, end] = [end, start];
  if (end - start < 0.3) return null;
  return { start, end };
}

function updateTrimUI() {
  const line = $("trim-line");
  const btn = $("btn-trim");
  const uncut = $("btn-uncut");
  if (!line || !btn) return;
  const range = trimRange();
  const inMark = fmtTime(state.trimIn);
  const outMark = fmtTime(state.trimOut);
  if (range) line.textContent = `in ${inMark} · out ${outMark} · ${fmtTime(range.end - range.start)}`;
  else line.textContent = `in ${inMark} · out ${outMark}`;
  btn.disabled = state.trimming || !range;
  btn.textContent = state.trimming ? "Cutting…" : "Cut";
  const clip = currentClip();
  if (uncut) {
    uncut.disabled = state.trimming || !clip?.trimmed;
    uncut.textContent = state.trimming && clip?.trimmed ? "Restoring…" : "Uncut";
  }
}

function markIn() {
  if (!state.playerOpen || !isClipItem(currentClip())) return;
  state.trimIn = $("player").currentTime;
  updateTrimUI();
}

function markOut() {
  if (!state.playerOpen || !isClipItem(currentClip())) return;
  state.trimOut = $("player").currentTime;
  updateTrimUI();
}

function reloadClip(payload, clipId) {
  applyPayload(payload);
  state.trimIn = null;
  state.trimOut = null;
  $("player").removeAttribute("data-clip");
  const next = (payload.clips || []).find((row) => row.id === clipId) || currentClip();
  if (next) showClip(next, { autoplay: true });
  renderSummary();
  highlightCurrent();
  updateTrimUI();
}

async function cutCurrentClip() {
  const clip = currentClip();
  const range = trimRange();
  if (!isClipItem(clip) || !range || state.trimming) return;
  state.trimming = true;
  updateTrimUI();
  try {
    const payload = await api(reviewApi("/trim"), {
      method: "POST",
      body: JSON.stringify({ id: clip.id, start: range.start, end: range.end }),
    });
    reloadClip(payload, clip.id);
  } catch (err) {
    $("source-line").textContent = String(err.message || err);
  }
  state.trimming = false;
  updateTrimUI();
}

async function uncutCurrentClip() {
  const clip = currentClip();
  if (!isClipItem(clip) || !clip?.trimmed || state.trimming) return;
  state.trimming = true;
  updateTrimUI();
  try {
    const payload = await api(reviewApi("/uncut"), {
      method: "POST",
      body: JSON.stringify({ id: clip.id }),
    });
    reloadClip(payload, clip.id);
  } catch (err) {
    $("source-line").textContent = String(err.message || err);
  }
  state.trimming = false;
  updateTrimUI();
}

function pendingLocalClips() {
  return filteredClips().filter((clip) => !clip.local);
}

function syncLabel(prefix, n, total) {
  if (!n) return prefix;
  return `${prefix} · ${n}/${total}`;
}

function updateSyncButton() {
  const btn = $("btn-sync-local");
  if (!btn) return;
  const ready = Boolean(state.payload?.gcsReady);
  btn.classList.toggle("hidden", !ready);
  if (state.syncing) {
    btn.disabled = false;
    btn.textContent = syncLabel("Cancel sync", state.syncDone, state.syncTotal);
    return;
  }
  const n = pendingLocalClips().length;
  btn.disabled = n === 0;
  if (!n) btn.textContent = "Synced local";
  else if (
    state.source === "all" &&
    state.filter === "all" &&
    state.game === "all" &&
    state.event === "all" &&
    !state.matchup.trim()
  ) {
    btn.textContent = `Sync ${n} to local`;
  } else {
    btn.textContent = `Sync ${n} filtered to local`;
  }
}

function updateExportButtons() {
  const portraits = $("btn-portraits");
  const decorate = $("btn-decorate");
  const music = $("btn-music");
  const generateTitles = $("btn-generate-titles");
  const stitch = $("btn-stitch");
  const syncPicks = $("btn-sync-picks");
  const refresh = $("btn-refresh-exports");
  const postWrap = $("post-platforms");
  const postBtn = $("btn-post");
  const syncPostTitles = $("btn-sync-post-titles");
  const trackWrap = $("track-pick-wrap");
  const trackPick = $("track-pick");
  const hint = $("export-hint");
  if (!hint) return;
  const picks = pickClips();
  const missingLocal = pendingPickClips();
  const running = Boolean(state.jobRunning || state.syncing);
  const gcsReady = Boolean(state.payload?.gcsReady);
  const clipView = state.view === "clips";
  const needWeaves = missingWeaveGames();
  const needPortraits = missingPortraits();
  const needDecorated = missingDecorated();
  const needMusic = missingMusic();
  const trackId = selectedTrackId();

  $("source-tabs").classList.toggle("hidden", !isClipView());
  $("toolbar").classList.toggle("hidden", !isClipView());

  if (syncPicks) {
    syncPicks.classList.toggle("hidden", !clipView || !gcsReady);
    if (state.syncing) {
      syncPicks.disabled = false;
      syncPicks.textContent = syncLabel("Cancel sync", state.syncDone, state.syncTotal);
    } else {
      syncPicks.disabled = missingLocal.length === 0;
      syncPicks.textContent = missingLocal.length ? `Sync ${missingLocal.length} picks` : "Picks synced";
    }
  }
  if (stitch) {
    stitch.classList.toggle("hidden", state.view !== "stitched");
    stitch.disabled = running || !picks.length || missingLocal.length > 0 || needWeaves.length === 0;
    if (state.jobRunning && state.jobLabel === "review-stitch") stitch.textContent = "Stitching…";
    else stitch.textContent = needWeaves.length ? `Stitch ${needWeaves.length} games` : "Stitched";
  }
  if (portraits) {
    portraits.classList.toggle("hidden", state.view !== "portraits");
    portraits.disabled = running || needPortraits.length === 0;
    if (state.jobRunning && state.jobLabel === "review-portraits") portraits.textContent = "Making portraits…";
    else portraits.textContent = needPortraits.length ? `Make ${needPortraits.length} portraits` : "Portraits ready";
  }
  if (decorate) {
    decorate.classList.toggle("hidden", state.view !== "decorate");
    decorate.disabled = running || needDecorated.length === 0;
    if (state.jobRunning && state.jobLabel === "review-decorate") decorate.textContent = "Decorating…";
    else decorate.textContent = needDecorated.length ? `Decorate ${needDecorated.length}` : "Decorated";
  }
  if (trackWrap) {
    trackWrap.classList.toggle("hidden", state.view !== "music");
    if (state.view === "music") fillTrackSelect(trackPick, trackId);
    const previewBtn = $("btn-track-preview");
    if (previewBtn) {
      previewBtn.dataset.trackId = trackId || "";
      previewBtn.disabled = !trackId;
      syncTrackPreviewButtons();
    }
  }
  if (music) {
    music.classList.toggle("hidden", state.view !== "music");
    music.disabled = running || needMusic.length === 0 || !trackId;
    if (state.jobRunning && state.jobLabel === "review-music") {
      music.textContent = state.mixTargetId ? "Mixing…" : "Mixing all…";
    } else if (state.mixStatus === "done" && !needMusic.length) {
      music.textContent = "Music ready";
    } else {
      music.textContent = needMusic.length ? `Add music to ${needMusic.length}` : "Music ready";
    }
  }
  const untitled = titlesViewItems().filter((item) => !titleIsGenerated(titleRecord(item)));
  if (generateTitles) {
    generateTitles.classList.toggle("hidden", state.view !== "titles");
    generateTitles.disabled = running || state.generatingTitles || !titlesViewItems().length || !untitled.length;
    if (state.generatingTitles) {
      generateTitles.textContent = "Generating…";
    } else if (!titlesViewItems().length) {
      generateTitles.textContent = "Generate hooks";
    } else if (!untitled.length) {
      generateTitles.textContent = "All hooked";
    } else {
      generateTitles.textContent = `Generate ${untitled.length} hooks`;
    }
  }
  const postItems = postViewItems();
  const unposted = postItems.filter((item) => !postIsDone(item));
  const platforms = chosenPlatforms();
  if (postWrap) {
    postWrap.classList.toggle("hidden", state.view !== "post");
    for (const platform of ["youtube", "tiktok"]) {
      const box = $(`post-${platform}`);
      if (!box) continue;
      const ready = platformReady(platform);
      box.checked = Boolean(state.postPlatforms[platform] && ready);
      box.disabled = !ready;
      const label = box.closest("label");
      if (label) label.title = ready ? "" : postingInfo(platform).hint || `${platform} is not set up yet`;
    }
  }
  if (syncPostTitles) {
    syncPostTitles.classList.toggle("hidden", state.view !== "post");
    syncPostTitles.disabled = running || !postItems.length;
  }
  if (postBtn) {
    postBtn.classList.toggle("hidden", state.view !== "post");
    postBtn.disabled = running || !platforms.length || !unposted.length;
    if (state.jobRunning && state.jobLabel === "review-post") {
      postBtn.textContent = state.mixTargetId ? "Posting…" : "Posting all…";
    } else if (!platforms.length) {
      postBtn.textContent = "Post";
    } else if (!unposted.length) {
      postBtn.textContent = postItems.length ? "All posted" : "Post";
    } else {
      postBtn.textContent = `Post ${unposted.length}`;
    }
  }
  if (refresh) refresh.classList.toggle("hidden", isClipView());

  const jobHint = state.jobRunning ? (JOB_PROGRESS[state.jobLabel] || {}).hint : "";
  if (jobHint) {
    hint.textContent = jobHint;
  } else if (state.view === "stitched") {
    if (!picks.length) hint.textContent = "Rate godly / excellent on Clips, then stitch one weave per game.";
    else if (missingLocal.length) hint.textContent = `Sync ${missingLocal.length} of ${picks.length} picks locally first.`;
    else if (needWeaves.length) hint.textContent = `${needWeaves.length} pick games need a stitch.`;
    else hint.textContent = `${(state.payload?.exports?.weaves || []).length} stitched games.`;
  } else if (state.view === "portraits") {
    if (!(state.payload?.exports?.weaves || []).length) hint.textContent = "Stitch games first, then make dry 9:16 portraits.";
    else if (needPortraits.length) hint.textContent = `${needPortraits.length} stitched games need a portrait.`;
    else hint.textContent = `${(state.payload?.exports?.portraits || []).length} dry portraits.`;
  } else if (state.view === "decorate") {
    if (!(state.payload?.exports?.portraits || []).length) hint.textContent = "Make portraits first, then decorate.";
    else if (needDecorated.length) hint.textContent = `${needDecorated.length} dry portraits are not decorated yet.`;
    else hint.textContent = `${(state.payload?.exports?.decorated || []).length} decorated portraits.`;
  } else if (state.view === "music") {
    if (!(state.payload?.exports?.decorated || []).length) hint.textContent = "Decorate first, then pick a track and mix.";
    else if (!trackId) hint.textContent = "Adopt tracks into assets/music/pool.json, then preview and mix here.";
    else if (needMusic.length) hint.textContent = `Preview a track with ▶, then mix ${needMusic.length} decorated portraits. Remix a card to swap songs.`;
    else hint.textContent = `${(state.payload?.exports?.music || []).length} portraits have music in post/.`;
  } else if (state.view === "titles") {
    if (!(state.payload?.exports?.decorated || []).length) hint.textContent = "Decorate portraits first, then generate Shorts hooks here.";
    else if (untitled.length) hint.textContent = `${untitled.length} game${untitled.length === 1 ? "" : "s"} need hooks. Add context, Generate — pick with 1–5 (~30s).`;
    else hint.textContent = `${(state.payload?.exports?.decorated || []).length} decorated portraits have hook options. Press 1–5 to pick.`;
  } else if (state.view === "post") {
    const missing = ["youtube", "tiktok"].filter((p) => !platformReady(p));
    if (!postItems.length) hint.textContent = "Add music first — the mix in post/ is what gets uploaded.";
    else if (!platforms.length) {
      hint.textContent = missing.length
        ? `Set up ${missing.join(" and ")} first: pip install -r requirements-post.txt, then python post_short.py --login ${missing[0]}.`
        : "Tick YouTube or TikTok to choose where these go.";
    } else if (unposted.length) {
      hint.textContent = `${unposted.length} of ${postItems.length} not uploaded. YouTube lands private, TikTok lands in your inbox — you still publish from your phone.`;
    } else {
      hint.textContent = `${postItems.length} uploaded. Finish them in YouTube Studio and the TikTok inbox.`;
    }
  } else if (state.view === "classifications") {
    hint.textContent = "Classification table for the current clip filter.";
  } else if (!picks.length) {
    hint.textContent = "Rate godly / excellent, then open Stitched.";
  } else if (missingLocal.length) {
    hint.textContent = `${missingLocal.length} of ${picks.length} picks still on GCS. Sync them before stitching.`;
  } else {
    hint.textContent = `${picks.length} picks ready · Stitched → Portraits → Decorate → Music.`;
  }
}

async function reloadReview() {
  if (!state.vodId) return;
  const q = state.dayKey ? `?day=${encodeURIComponent(state.dayKey)}` : "";
  applyPayload(await api(`/api/review/${encodeURIComponent(state.vodId)}${q}`));
  render();
}

function jobProgress(jobId) {
  return JOB_PROGRESS[jobId] || { verb: "Working…", hint: "A local job is still running. Refresh is fine — this stays until it finishes." };
}

function showActiveJob(job) {
  if (!job || !job.running) return;
  activeJobSeen = true;
  state.jobRunning = true;
  state.jobLabel = job.job || state.jobLabel;
  if (job.job === "review-music") state.mixStatus = "running";
  const out = $("job-out");
  if (!out) return;
  out.classList.remove("hidden");
  const head = jobProgress(job.job).hint;
  const tail = String(job.logTail || "").trim();
  out.textContent = tail ? `${head}\n\n${tail}` : head;
}

let activeJobTimer = 0;
let activeJobSeen = false;

async function refreshActiveJob() {
  if (!state.vodId || state.jobPosting) return;
  let job = null;
  try {
    const data = await api(`/api/jobs/active?vod_id=${encodeURIComponent(state.vodId)}`);
    job = data.job || null;
  } catch {
    return;
  }
  if (job && job.running) {
    showActiveJob(job);
    updateExportButtons();
    if (state.view === "music" || state.view === "post") renderGrid();
    return;
  }
  if (!activeJobSeen && !state.jobRunning) return;
  const label = state.jobLabel;
  activeJobSeen = false;
  state.jobRunning = false;
  state.jobLabel = "";
  if (label === "review-music") state.mixStatus = "done";
  await reloadReview();
  const out = $("job-out");
  if (out) {
    out.classList.remove("hidden");
    const done = jobProgress(label).verb.replace("…", "");
    out.textContent = `${done} done.`;
  }
  updateExportButtons();
}

function startActiveJobPoll() {
  if (activeJobTimer) return;
  activeJobTimer = window.setInterval(() => {
    refreshActiveJob().catch(() => {});
  }, 3000);
}

function reviewJobExtra() {
  const extra = [];
  if (state.dayKey && state.dayKey !== "local" && state.vodId) {
    extra.push("--dataset-dir", `data/${state.dayKey}_${state.vodId}`);
  }
  return extra;
}

async function runReviewJob(jobId, extra = [], { mixTargetId = "" } = {}) {
  const out = $("job-out");
  if (!state.vodId || state.jobRunning) return null;
  out.classList.remove("hidden");
  const mixing = jobId === "review-music";
  const posting = jobId === "review-post";
  const progress = jobProgress(jobId);
  out.textContent = progress.verb.toLowerCase();
  state.jobRunning = true;
  state.jobLabel = jobId;
  state.jobPosting = true;
  state.mixTargetId = mixTargetId || "";
  state.mixStatus = mixing ? "running" : "";
  updateExportButtons();
  if (state.view === "music") renderGrid();
  let result = null;
  try {
    result = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({
        job: jobId,
        vodId: state.vodId,
        dayKey: state.dayKey && state.dayKey !== "local" ? state.dayKey : "",
        extra: [...reviewJobExtra(), ...extra],
        where: "local",
      }),
    });
    const waiting = Boolean(result.background);
    const lines = [
      result.commandLine,
      result.ok
        ? waiting
          ? progress.hint
          : mixing
            ? "music mix done"
            : posting
              ? "upload done"
              : "done"
        : "failed",
    ];
    if (result.log) lines.push(`log ${result.log}`);
    if (result.stdout) lines.push(result.stdout.trim());
    if (result.stderr) lines.push(result.stderr.trim());
    out.textContent = lines.filter(Boolean).join("\n\n");
    if (waiting && result.ok) {
      activeJobSeen = true;
      showActiveJob({
        running: true,
        job: jobId,
        pid: result.pid,
        logTail: result.stdout || "",
      });
    }
    if (mixing && result.ok) {
      state.mixStatus = "done";
      await reloadReview();
      if (state.mixTargetId) {
        renderGrid();
        setTimeout(() => {
          if (state.mixStatus === "done") {
            state.mixStatus = "";
            state.mixTargetId = "";
            if (state.view === "music") renderGrid();
            updateExportButtons();
          }
        }, 1600);
      } else {
        state.mixStatus = "";
        state.mixTargetId = "";
      }
    }
    if (posting) {
      // Reload either way: a partial run still records what did upload.
      await reloadReview();
      state.mixTargetId = "";
    }
  } catch (err) {
    out.textContent = String(err.message || err);
    state.mixStatus = "";
    state.mixTargetId = "";
  }
  state.jobPosting = false;
  if (!(result && result.ok && result.background)) {
    state.jobRunning = false;
    state.jobLabel = "";
  }
  updateExportButtons();
  if (state.view === "music") renderGrid();
  return result;
}

function mixOne(item, trackId) {
  const track = String(trackId || selectedTrackId() || "").trim();
  const only = item?.gameId || item?.weaveStem;
  if (!track || !only || state.jobRunning) return;
  setMusicTrack(track);
  runReviewJob("review-music", ["--track", track, "--only", only, "--force"], {
    mixTargetId: item.id,
  });
}

function mixPending() {
  const track = selectedTrackId();
  if (!track || state.jobRunning) return;
  setMusicTrack(track);
  runReviewJob("review-music", ["--track", track], { mixTargetId: "" });
}

function cancelSync() {
  if (state.syncAbort) state.syncAbort.abort();
}

async function readNdjson(res, onEvent) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const lines = buf.split("\n");
    buf = lines.pop() || "";
    for (const line of lines) {
      if (!line.trim()) continue;
      onEvent(JSON.parse(line));
    }
  }
  if (buf.trim()) onEvent(JSON.parse(buf));
}

async function syncClipIds(ids) {
  if (!state.vodId || !ids.length || state.syncing) return;
  const ac = new AbortController();
  state.syncAbort = ac;
  state.syncing = true;
  state.syncDone = 0;
  state.syncTotal = ids.length;
  updateSyncButton();
  updateExportButtons();
  let failed = 0;
  let done = 0;
  let aborted = false;
  try {
    const res = await fetch(reviewApi("/pull"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
      signal: ac.signal,
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      const detail = data.detail || res.statusText;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    await readNdjson(res, (ev) => {
      if (ev.event === "error") throw new Error(ev.error || "sync failed");
      if (ev.event === "progress") {
        if (ev.ok === false) failed += 1;
        else if (ev.id) {
          const clip = (state.payload.clips || []).find((row) => row.id === ev.id);
          if (clip) {
            clip.local = true;
            clip.gcs = false;
          }
        }
        done = ev.done || done;
        state.syncDone = done;
        state.syncTotal = ev.total || ids.length;
        renderSummary();
        renderSourceTabs();
        updateSyncButton();
        updateExportButtons();
      } else if (ev.event === "done") {
        done = ev.done || done;
        failed = ev.failed || failed;
      }
    });
  } catch (err) {
    aborted = err?.name === "AbortError";
    if (!aborted) $("source-line").textContent = String(err.message || err);
  }
  state.syncAbort = null;
  state.syncing = false;
  state.syncDone = 0;
  state.syncTotal = 0;
  try {
    if (state.vodId) {
      const q = state.dayKey ? `?day=${encodeURIComponent(state.dayKey)}` : "";
      applyPayload(await api(`/api/review/${encodeURIComponent(state.vodId)}${q}`));
    }
  } catch (err) {
    if (!aborted) $("source-line").textContent = String(err.message || err);
  }
  render();
  if (aborted) $("source-line").textContent = `Stopped at ${done} clips`;
  else if (failed) $("source-line").textContent = `Synced ${done}, ${failed} failed`;
}

async function syncFilteredLocal() {
  await syncClipIds(pendingLocalClips().map((clip) => clip.id));
}

async function syncPicksLocal() {
  await syncClipIds(pendingPickClips().map((clip) => clip.id));
}

function syncMatchupInput() {
  const input = $("matchup-search");
  if (!input || document.activeElement === input) return;
  if (input.value !== state.matchup) input.value = state.matchup;
}

function render() {
  renderSummary();
  renderSourceTabs();
  renderViewTabs();
  renderFilters();
  syncMatchupInput();
  renderGrid();
  renderClassificationsView();
  renderTitlesView();
  renderPostView();
  updateSyncButton();
  updateExportButtons();
  if (state.playerOpen) {
    const clip = currentClip();
    if (!clip) closePlayer();
    else {
      const player = $("player");
      const same = player.getAttribute("data-clip") === clip.id;
      if (!same) showClip(clip, { autoplay: true });
      else highlightCurrent();
    }
  }
}

let matchupTimer = null;
$("matchup-search").addEventListener("input", (ev) => {
  clearTimeout(matchupTimer);
  matchupTimer = setTimeout(() => {
    const next = ev.target.value.trim();
    if (next === state.matchup.trim()) return;
    state.matchup = ev.target.value;
    state.index = 0;
    syncUrl();
    render();
  }, 150);
});
$("matchup-search").addEventListener("keydown", (ev) => {
  if (ev.key !== "Escape") return;
  ev.target.value = "";
  state.matchup = "";
  state.index = 0;
  syncUrl();
  render();
});

$("btn-days").addEventListener("click", () => {
  if ($("day-nav").classList.contains("hidden")) openDayNav();
  else closeDayNav();
});
$("day-nav-backdrop").addEventListener("click", closeDayNav);
$("auto-advance").addEventListener("change", (ev) => {
  state.autoAdvance = ev.target.checked;
  localStorage.setItem("reviewAutoAdvance", state.autoAdvance ? "1" : "0");
});
$("speed-seg").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-speed]");
  if (!btn) return;
  state.speed = Number(btn.dataset.speed);
  for (const el of $("speed-seg").querySelectorAll("button")) {
    el.classList.toggle("on", el === btn);
  }
  $("player").playbackRate = state.speed;
});
$("btn-play-day").addEventListener("click", () => {
  if (state.playDay) {
    closePlayer();
    return;
  }
  const clip = currentClip() || viewItems()[0];
  if (!clip) return;
  openPlayer(clip.id, { playDay: true });
});
$("btn-sync-local").addEventListener("click", () => {
  if (state.syncing) cancelSync();
  else syncFilteredLocal();
});
$("btn-sync-picks").addEventListener("click", () => {
  if (state.syncing) cancelSync();
  else syncPicksLocal();
});
$("btn-stitch").addEventListener("click", () => runReviewJob("review-stitch"));
$("btn-portraits").addEventListener("click", () => runReviewJob("review-portraits"));
$("btn-decorate").addEventListener("click", () => runReviewJob("review-decorate"));
$("btn-generate-titles").addEventListener("click", () => generateAllTitles());
$("btn-music").addEventListener("click", () => mixPending());
$("track-pick").addEventListener("change", (ev) => {
  setMusicTrack(ev.target.value);
  const previewBtn = $("btn-track-preview");
  if (previewBtn) previewBtn.dataset.trackId = ev.target.value || "";
  if (state.musicPreview.playing && state.musicPreview.trackId !== ev.target.value) stopTrackPreview();
  renderGrid();
  updateExportButtons();
});
$("btn-track-preview")?.addEventListener("click", () => {
  const trackId = $("track-pick")?.value || "";
  $("btn-track-preview").dataset.trackId = trackId;
  toggleTrackPreview(trackId);
});
$("track-preview")?.addEventListener("ended", () => stopTrackPreview());
$("btn-post").addEventListener("click", () => postPending());
$("btn-sync-post-titles").addEventListener("click", () => syncPostTitles());
for (const platform of ["youtube", "tiktok"]) {
  $(`post-${platform}`)?.addEventListener("change", (ev) => {
    state.postPlatforms[platform] = ev.target.checked;
    localStorage.setItem(
      platform === "youtube" ? "reviewPostYoutube" : "reviewPostTiktok",
      ev.target.checked ? "1" : "0",
    );
    renderPostView();
    updateExportButtons();
  });
}
$("btn-refresh-exports").addEventListener("click", () => reloadReview());
$("seek-row").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-seek]");
  if (!btn) return;
  seekBy(Number(btn.dataset.seek));
});
$("btn-mark-in").addEventListener("click", markIn);
$("btn-mark-out").addEventListener("click", markOut);
$("btn-reveal-local").addEventListener("click", () => revealCurrentClip());
$("btn-trim").addEventListener("click", () => cutCurrentClip());
$("btn-uncut").addEventListener("click", () => uncutCurrentClip());
$("player").addEventListener("timeupdate", updatePlayerTime);
$("player").addEventListener("loadedmetadata", updatePlayerTime);
$("player").addEventListener("seeked", updatePlayerTime);
$("rate-row").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-rating]");
  if (!btn) return;
  setRating(btn.dataset.rating);
});
$("player-mode").addEventListener("click", (ev) => {
  if (ev.target === $("player-mode")) closePlayer();
});
$("player").addEventListener("ended", () => {
  if (!state.playDay) return;
  const items = viewItems();
  if (state.index < items.length - 1) step(1);
});

document.addEventListener("keydown", (ev) => {
  const tag = ev.target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
  const navOpen = !$("day-nav").classList.contains("hidden");
  if (ev.key === "Escape") {
    if (navOpen) {
      closeDayNav();
      return;
    }
    closePlayer();
    return;
  }
  if (navOpen) return;
  if (state.playerOpen && (ev.key === "ArrowRight" || ev.key === ".")) {
    ev.preventDefault();
    seekBy(3);
    return;
  }
  if (state.playerOpen && (ev.key === "ArrowLeft" || ev.key === ",")) {
    ev.preventDefault();
    seekBy(-3);
    return;
  }
  if (state.playerOpen && isClipItem(currentClip()) && (ev.key === "[" || ev.key === "i" || ev.key === "I")) {
    ev.preventDefault();
    markIn();
    return;
  }
  if (state.playerOpen && isClipItem(currentClip()) && (ev.key === "]" || ev.key === "o" || ev.key === "O")) {
    ev.preventDefault();
    markOut();
    return;
  }
  if (state.playerOpen && isClipItem(currentClip()) && (ev.key === "c" || ev.key === "C")) {
    ev.preventDefault();
    cutCurrentClip();
    return;
  }
  if (state.playerOpen && isClipItem(currentClip()) && (ev.key === "u" || ev.key === "U")) {
    ev.preventDefault();
    uncutCurrentClip();
    return;
  }
  if (ev.key === "ArrowRight" || ev.key === "j" || ev.key === "J") {
    ev.preventDefault();
    step(1);
    return;
  }
  if (ev.key === "ArrowLeft" || ev.key === "k" || ev.key === "K") {
    ev.preventDefault();
    step(-1);
    return;
  }
  if (ev.key === " ") {
    ev.preventDefault();
    togglePlay();
    return;
  }
  if (isClipItem(currentClip())) {
    if (ev.key === "1") setRating("reject");
    if (ev.key === "2") setRating("keep");
    if (ev.key === "3") setRating("excellent");
    if (ev.key === "5") setRating("godly");
    if (ev.key === "4") setRating("manual_edit");
    if (ev.key === "0") setRating("clear");
  } else if (state.view === "titles") {
    const clip = currentClip();
    const rec = clip ? titleRecord(clip) : null;
    const opts = titleHookOptions(rec).slice(0, 5);
    const n = Number(ev.key);
    if (n >= 1 && n <= 5 && opts[n - 1] && clip) {
      ev.preventDefault();
      setTitlePick(clip.weaveStem, opts[n - 1].text, opts[n - 1].style);
      renderTitlePanel(clip);
    }
  }
  if (ev.key === "f" || ev.key === "F") {
    ev.preventDefault();
    if (state.playerOpen) toggleFullscreen();
  }
});

async function boot() {
  const route = parseRoute();
  const revealBtn = $("btn-reveal-local");
  if (revealBtn) revealBtn.textContent = REVEAL_LABEL;
  state.vodId = route.vodId;
  state.dayKey = route.dayKey;
  state.filter = route.filter;
  state.classFilter = route.classFilter;
  state.view = route.view;
  state.source = route.source;
  state.game = route.game;
  state.event = route.event;
  state.matchup = route.matchup;
  state.autoAdvance = localStorage.getItem("reviewAutoAdvance") !== "0";
  $("matchup-search").value = state.matchup;
  $("auto-advance").checked = state.autoAdvance;
  loadCatalog();
  if (!route.vodId) {
    $("day-title").textContent = "Clip review";
    $("source-line").textContent = "Pick a day";
    openDayNav();
    return;
  }
  try {
    const q = route.dayKey ? `?day=${encodeURIComponent(route.dayKey)}` : "";
    const payload = await api(`/api/review/${encodeURIComponent(route.vodId)}${q}`);
    applyPayload(payload);
    render();
    startActiveJobPoll();
  } catch (err) {
    $("source-line").textContent = String(err.message || err);
    openDayNav();
  }
}

boot();
