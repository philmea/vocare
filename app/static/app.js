"use strict";

const $ = (id) => document.getElementById(id);
const audio = $("audio");
const SPEEDS = [0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.5, 1.75, 2.0, 2.25, 2.5];
const SLEEP_OPTIONS = [0, 15, 30, 45, 60, -1]; // minutes; -1 = end of chapter

// ------------------------------------------------------------ storage
function load(key, fallback) {
  try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; } catch { return fallback; }
}
function save(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage unavailable */ }
}

let config = null;
let settings = load("rr.settings", { provider: null, voice: null, speed: 1.0 });
let library = load("rr.library", {});   // fid -> {id,title,author,cover,chapters,progress,addedAt,lastPlayed}
let current = null;                     // {fid, idx, part, total, provider, voice}
let pendingSeek = 0;
let statusTimer = null;
let sleep = { mode: 0, until: 0 };
let viewFid = null;

const saveLibrary = () => save("rr.library", library);
const saveSettings = () => save("rr.settings", settings);

// ------------------------------------------------------------ api
async function api(path, opts = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...opts });
  if (res.status === 401 && !path.startsWith("/api/login")) {
    await showLogin();
    return api(path, opts);
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `Request failed (${res.status})`);
  return body;
}

function showLogin() {
  return new Promise((resolve) => {
    const dlg = $("loginDlg");
    $("loginForm").onsubmit = async (e) => {
      e.preventDefault();
      try {
        await api("/api/login", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ password: $("loginPw").value }),
        });
        dlg.close();
        resolve();
      } catch (err) {
        showMsg($("loginMsg"), err.message);
      }
    };
    if (!dlg.open) dlg.showModal();
  });
}

function showMsg(el, text, ok = false) {
  el.textContent = text;
  el.classList.toggle("ok", ok);
  el.hidden = !text;
}

// ------------------------------------------------------------ settings
function provider() { return config.providers.find((p) => p.id === settings.provider) || config.providers[0]; }

function normalizeSettings() {
  const p = provider();
  if (!p) return;
  settings.provider = p.id;
  if (!p.voices.some((v) => v.id === settings.voice)) settings.voice = p.default_voice;
  saveSettings();
}

function renderSettings() {
  const pSel = $("providerSel"), vSel = $("voiceSel");
  pSel.innerHTML = config.providers.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  pSel.value = settings.provider;
  vSel.innerHTML = provider().voices.map((v) => `<option value="${esc(v.id)}">${esc(v.name)}</option>`).join("");
  vSel.value = settings.voice;
}

$("settingsBtn").onclick = () => { renderSettings(); $("settingsDlg").showModal(); };
$("providerSel").onchange = (e) => { settings.provider = e.target.value; settings.voice = null; normalizeSettings(); renderSettings(); };
$("voiceSel").onchange = (e) => { settings.voice = e.target.value; saveSettings(); };
$("previewBtn").onclick = () => {
  const pa = $("previewAudio");
  pa.src = `/api/preview?provider=${encodeURIComponent(settings.provider)}&voice=${encodeURIComponent(settings.voice)}`;
  $("previewBtn").textContent = "Loading…";
  pa.play().catch(() => {});
  pa.onplaying = () => { $("previewBtn").textContent = "Preview voice"; };
  pa.onerror = () => { $("previewBtn").textContent = "Preview failed"; };
};

// ------------------------------------------------------------ views
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function route() {
  const m = location.hash.match(/^#\/f\/(\d+)/);
  if (m && library[m[1]]) { showFiction(m[1]); return; }
  if (location.hash && location.hash !== "#/") history.replaceState(null, "", "#/");
  showLibrary();
}
window.addEventListener("hashchange", route);
$("backBtn").onclick = () => { location.hash = "#/"; };

function showLibrary() {
  viewFid = null;
  $("libraryView").hidden = false;
  $("fictionView").hidden = true;
  $("backBtn").hidden = true;
  $("viewTitle").textContent = "Library";
  const items = Object.values(library).sort((a, b) => (b.lastPlayed || b.addedAt) - (a.lastPlayed || a.addedAt));
  $("libraryEmpty").hidden = items.length > 0;
  $("libraryList").innerHTML = items.map((f) => {
    const p = f.progress;
    const ch = p && f.chapters[p.idx];
    const sub = ch ? `Ch ${p.idx + 1} of ${f.chapters.length} · ${esc(ch.title)}` : `${f.chapters.length} chapters`;
    return `<li data-fid="${f.id}"><img src="${esc(f.cover || "")}" alt="" loading="lazy">
      <div><div class="t">${esc(f.title)}</div><div class="s">${esc(f.author)}</div><div class="s">${sub}</div></div></li>`;
  }).join("");
}
$("libraryList").onclick = (e) => {
  const li = e.target.closest("li[data-fid]");
  if (li) location.hash = `#/f/${li.dataset.fid}`;
};

$("addForm").onsubmit = async (e) => {
  e.preventDefault();
  showMsg($("addMsg"), "Loading…", true);
  try {
    const f = await api(`/api/fiction?url=${encodeURIComponent($("addUrl").value)}`);
    const existing = library[f.id];
    library[f.id] = { ...f, progress: existing?.progress || null, addedAt: existing?.addedAt || Date.now(), lastPlayed: existing?.lastPlayed || 0 };
    saveLibrary();
    $("addUrl").value = "";
    showMsg($("addMsg"), "");
    if (location.hash === `#/f/${f.id}`) route(); else location.hash = `#/f/${f.id}`;
  } catch (err) {
    showMsg($("addMsg"), err.message);
  }
};

function showFiction(fid) {
  viewFid = fid;
  const f = library[fid];
  $("libraryView").hidden = true;
  $("fictionView").hidden = false;
  $("backBtn").hidden = false;
  $("viewTitle").textContent = f.title;
  $("ficCover").src = f.cover || "";
  $("ficTitle").textContent = f.title;
  $("ficAuthor").textContent = f.author;
  showMsg($("ficMsg"), f.chapters.length ? "" : "No chapters are available (the story may be stubbed).");
  renderChapters();
  refreshFiction(fid, true);
}

function renderChapters() {
  const f = library[viewFid];
  if (!f) return;
  const p = f.progress;
  const isCurrent = (i) => current && current.fid === String(f.id) && current.idx === i;
  $("chapterList").innerHTML = f.chapters.map((c, i) =>
    `<li data-idx="${i}" class="${isCurrent(i) ? "current" : p && i < p.idx ? "done" : ""}">
      <span class="n">${i + 1}</span><span class="ct">${esc(c.title)}</span></li>`).join("");
  const btn = $("continueBtn");
  if (p && f.chapters[p.idx]) btn.textContent = `Continue ch. ${p.idx + 1}`;
  else btn.textContent = "Start listening";
  btn.disabled = !f.chapters.length;
}

async function refreshFiction(fid, quiet) {
  try {
    const f = await api(`/api/fiction?url=${fid}${quiet ? "" : "&refresh=1"}`);
    const old = library[fid];
    if (!old) return;
    // Keep the position on the same chapter even if chapters were inserted/removed.
    let progress = old.progress;
    if (progress && old.chapters[progress.idx]) {
      const newIdx = f.chapters.findIndex((c) => c.id === old.chapters[progress.idx].id);
      progress = newIdx >= 0 ? { ...progress, idx: newIdx } : progress;
    }
    library[fid] = { ...old, ...f, progress };
    saveLibrary();
    if (viewFid === fid) {
      $("viewTitle").textContent = f.title;
      if (!quiet) showMsg($("ficMsg"), `Updated: ${f.chapters.length} chapters.`, true);
      renderChapters();
    }
  } catch (err) {
    if (!quiet && viewFid === fid) showMsg($("ficMsg"), err.message);
  }
}

$("refreshBtn").onclick = () => refreshFiction(viewFid, false);
$("removeBtn").onclick = () => {
  if (!confirm("Remove this story from your library?")) return;
  if (current && current.fid === viewFid) stopPlayback();
  delete library[viewFid];
  saveLibrary();
  location.hash = "#/";
};
$("continueBtn").onclick = () => {
  const f = library[viewFid];
  const p = f.progress;
  if (p && f.chapters[p.idx]) playChapter(viewFid, p.idx, p.part || 0, p.time || 0);
  else playChapter(viewFid, 0);
};
$("chapterList").onclick = (e) => {
  const li = e.target.closest("li[data-idx]");
  if (!li) return;
  const idx = Number(li.dataset.idx);
  const p = library[viewFid].progress;
  if (current && current.fid === viewFid && current.idx === idx) { togglePlay(); return; }
  if (p && p.idx === idx) playChapter(viewFid, idx, p.part || 0, p.time || 0);
  else playChapter(viewFid, idx);
};

// ------------------------------------------------------------ playback
function qs(c) { return `provider=${encodeURIComponent(c.provider)}&voice=${encodeURIComponent(c.voice)}`; }
function chapterOf(c) { return library[c.fid]?.chapters[c.idx]; }
function partUrl(c) { return `/api/audio/${c.fid}/${chapterOf(c).id}/${c.part}.mp3?${qs(c)}`; }

// Must be called synchronously from a tap so mobile browsers allow audio to start.
function playChapter(fid, idx, part = 0, time = 0) {
  const f = library[fid];
  if (!f || !f.chapters[idx]) return;
  current = { fid: String(fid), idx, part, total: null, state: "queued", ready: 0, provider: settings.provider, voice: settings.voice };
  loadPart(time, true);
  f.lastPlayed = Date.now();
  f.progress = { idx, part, time };
  saveLibrary();
  $("player").hidden = false;
  renderChapters();
  updateMediaSession();
  pollStatus();
}

function loadPart(time, autoplay) {
  pendingSeek = time || 0;
  audio.src = partUrl(current);
  audio.defaultPlaybackRate = settings.speed;
  audio.playbackRate = settings.speed;
  if (autoplay) audio.play().catch(() => {});
  renderPlayer();
}

function stopPlayback() {
  audio.pause();
  audio.removeAttribute("src");
  audio.load();
  current = null;
  clearTimeout(statusTimer);
  $("player").hidden = true;
}

function togglePlay() {
  if (!current) return;
  if (audio.paused) audio.play().catch(() => {}); else audio.pause();
}

function nextChapter() {
  if (!current) return;
  const f = library[current.fid];
  if (current.idx + 1 < f.chapters.length) playChapter(current.fid, current.idx + 1);
}

function prevChapter() {
  if (!current) return;
  // Like most players: restart this chapter unless we're near its start.
  if (current.part > 0 || audio.currentTime > 10) playChapter(current.fid, current.idx);
  else if (current.idx > 0) playChapter(current.fid, current.idx - 1);
}

function skip(seconds) {
  if (!current) return;
  const t = audio.currentTime + seconds;
  if (t < 0 && current.part > 0) {
    current.part -= 1;
    loadPart(Math.max(0, (current.prevPartDuration || 0) + t), !audio.paused);
  } else if (isFinite(audio.duration) && t >= audio.duration) {
    onEnded();
  } else {
    audio.currentTime = Math.max(0, t);
  }
}

async function onEnded() {
  if (!current) return;
  if (sleep.mode === -1) { setSleep(0); audio.pause(); advanceSavedOnly(); return; }
  if (current.total === null) await fetchStatus();
  if (current.total !== null && current.part + 1 < current.total) {
    current.prevPartDuration = audio.duration;
    current.part += 1;
    loadPart(0, true);
    saveProgress(true);
  } else {
    const f = library[current.fid];
    if (current.idx + 1 < f.chapters.length) {
      playChapter(current.fid, current.idx + 1);
    } else {
      f.progress = { idx: current.idx + 1, part: 0, time: 0 };
      saveLibrary();
      $("npSub").textContent = "You're all caught up.";
    }
  }
}

// When a sleep timer ends a chapter, remember the *next* part so we resume there.
function advanceSavedOnly() {
  const f = library[current.fid];
  if (current.total !== null && current.part + 1 < current.total) {
    current.part += 1;
    loadPart(0, false);
  } else if (current.idx + 1 < f.chapters.length) {
    current = { ...current, idx: current.idx + 1, part: 0, total: null, state: "queued", ready: 0 };
    loadPart(0, false);
    renderChapters();
    pollStatus();
  }
  saveProgress(true);
}

audio.addEventListener("ended", onEnded);
audio.addEventListener("loadedmetadata", () => {
  if (pendingSeek && pendingSeek < audio.duration) audio.currentTime = pendingSeek;
  pendingSeek = 0;
  audio.playbackRate = settings.speed;
  renderPlayer();
});
audio.addEventListener("error", () => {
  if (!current || !audio.getAttribute("src")) return;
  $("npSub").textContent = "Couldn't load audio — tap play to retry.";
  current.loadError = true;
});
for (const ev of ["play", "pause", "playing", "waiting"]) audio.addEventListener(ev, renderPlayer);
let lastSave = 0;
audio.addEventListener("timeupdate", () => {
  renderTime();
  if (Date.now() - lastSave > 5000) saveProgress();
  checkSleep();
});

function saveProgress(force) {
  if (!current) return;
  lastSave = Date.now();
  const f = library[current.fid];
  if (!f) return;
  f.progress = { idx: current.idx, part: current.part, time: force ? 0 : audio.currentTime };
  saveLibrary();
}

$("playBtn").onclick = () => {
  if (current && current.loadError) { current.loadError = false; loadPart(audio.currentTime || 0, true); return; }
  togglePlay();
};
$("prevBtn").onclick = prevChapter;
$("nextBtn").onclick = nextChapter;
$("backSkip").onclick = () => skip(-15);
$("fwdSkip").onclick = () => skip(30);
$("seek").oninput = (e) => {
  if (isFinite(audio.duration)) audio.currentTime = (e.target.value / 1000) * audio.duration;
};
$("speedBtn").onclick = () => {
  const i = SPEEDS.indexOf(settings.speed);
  settings.speed = SPEEDS[(i + 1) % SPEEDS.length];
  audio.defaultPlaybackRate = audio.playbackRate = settings.speed;
  saveSettings();
  renderPlayer();
};

// ------------------------------------------------------------ sleep timer
function setSleep(mode) {
  sleep = { mode, until: mode > 0 ? Date.now() + mode * 60000 : 0 };
  renderSleep();
}
function renderSleep() {
  const b = $("sleepBtn");
  if (sleep.mode === 0) b.textContent = "Sleep: off";
  else if (sleep.mode === -1) b.textContent = "Sleep: end of chapter";
  else b.textContent = `Sleep: ${Math.max(0, Math.ceil((sleep.until - Date.now()) / 60000))} min`;
}
function checkSleep() {
  if (sleep.mode > 0) {
    if (Date.now() >= sleep.until) { audio.pause(); setSleep(0); saveProgress(); }
    else renderSleep();
  }
}
$("sleepBtn").onclick = () => {
  const i = SLEEP_OPTIONS.indexOf(sleep.mode);
  setSleep(SLEEP_OPTIONS[(i + 1) % SLEEP_OPTIONS.length]);
};

// ------------------------------------------------------------ generation status
async function fetchStatus() {
  if (!current) return;
  const c = current;
  try {
    const st = await api(`/api/status/${chapterOf(c).id}?${qs(c)}`);
    if (c !== current) return;
    Object.assign(c, { total: st.total, ready: st.ready, state: st.state, error: st.error });
  } catch { /* transient */ }
}

function pollStatus() {
  clearTimeout(statusTimer);
  const c = current;
  const tick = async () => {
    if (c !== current) return;
    await fetchStatus();
    renderPlayer();
    if (c.state === "done") { prefetchNext(c); return; }
    if (c.state === "error") return;
    statusTimer = setTimeout(tick, 3000);
  };
  tick();
}

// Generate the next chapter in the background so it's ready when this one ends.
function prefetchNext(c) {
  const f = library[c.fid];
  const next = f.chapters[c.idx + 1];
  if (next) api(`/api/prepare/${c.fid}/${next.id}?${qs(c)}`, { method: "POST" }).catch(() => {});
}

// ------------------------------------------------------------ player UI
function fmt(t) {
  if (!isFinite(t)) return "0:00";
  t = Math.floor(t);
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  return (h ? `${h}:${String(m).padStart(2, "0")}` : m) + ":" + String(s).padStart(2, "0");
}

function renderTime() {
  $("tCur").textContent = fmt(audio.currentTime);
  $("tDur").textContent = fmt(audio.duration);
  if (isFinite(audio.duration) && document.activeElement !== $("seek")) {
    $("seek").value = Math.round((audio.currentTime / audio.duration) * 1000) || 0;
  }
  if ("mediaSession" in navigator && isFinite(audio.duration)) {
    try {
      navigator.mediaSession.setPositionState({ duration: audio.duration, position: Math.min(audio.currentTime, audio.duration), playbackRate: audio.playbackRate });
    } catch { /* unsupported */ }
  }
}

function renderPlayer() {
  if (!current) return;
  const f = library[current.fid];
  const ch = chapterOf(current);
  $("npTitle").textContent = ch ? ch.title : "";
  let sub = f.title;
  if (current.state === "error") sub = `Error: ${current.error || "generation failed"}`;
  else if (current.state === "fetching" || current.state === "queued") sub = "Fetching chapter…";
  else if (current.total !== null && current.ready < current.total) sub = `Generating audio… ${current.ready}/${current.total} parts ready`;
  else if (!audio.paused && audio.readyState < 3) sub = "Loading…";
  $("npSub").textContent = sub;
  $("tPart").textContent = current.total ? `Part ${current.part + 1} of ${current.total}` : "";
  $("playIcon").hidden = !audio.paused;
  $("pauseIcon").hidden = audio.paused;
  $("playBtn").setAttribute("aria-label", audio.paused ? "Play" : "Pause");
  $("speedBtn").textContent = `${settings.speed}×`;
  renderTime();
  if ("mediaSession" in navigator) navigator.mediaSession.playbackState = audio.paused ? "paused" : "playing";
}

function updateMediaSession() {
  if (!("mediaSession" in navigator) || !current) return;
  const f = library[current.fid];
  const ch = chapterOf(current);
  navigator.mediaSession.metadata = new MediaMetadata({
    title: ch.title, artist: f.author, album: f.title,
    artwork: f.cover ? [{ src: f.cover, sizes: "400x600" }] : [],
  });
}

if ("mediaSession" in navigator) {
  const ms = navigator.mediaSession;
  const set = (action, fn) => { try { ms.setActionHandler(action, fn); } catch { /* unsupported */ } };
  set("play", () => audio.play());
  set("pause", () => audio.pause());
  set("seekbackward", (d) => skip(-(d.seekOffset || 15)));
  set("seekforward", (d) => skip(d.seekOffset || 30));
  set("previoustrack", prevChapter);
  set("nexttrack", nextChapter);
  set("seekto", (d) => { audio.currentTime = d.seekTime; });
}

// ------------------------------------------------------------ boot
(async function init() {
  try {
    config = await api("/api/config");
    if (config.auth_required && !config.authed) await showLogin();
  } catch (err) {
    document.querySelector("main").innerHTML = `<p class="msg">Couldn't reach the server: ${esc(err.message)}</p>`;
    return;
  }
  if (!config.providers.length) {
    showMsg($("addMsg"), "No text-to-speech service is configured on the server.");
  }
  normalizeSettings();
  route();
  // Show the player for the most recent story, ready to resume (not auto-playing).
  const recent = Object.values(library).filter((f) => f.progress && f.chapters[f.progress.idx])
    .sort((a, b) => (b.lastPlayed || 0) - (a.lastPlayed || 0))[0];
  if (recent && config.providers.length) {
    const p = recent.progress;
    current = { fid: String(recent.id), idx: p.idx, part: p.part || 0, total: null, state: "queued", ready: 0, provider: settings.provider, voice: settings.voice };
    $("player").hidden = false;
    loadPart(p.time || 0, false);
    updateMediaSession();
    renderChapters();
    pollStatus();
  }
})();
