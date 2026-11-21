// Live AI Subtitles - IINA plugin
// Poll-driven so it works regardless of when file/overlay events fire.
const { console, core, event, overlay, http, utils } = iina;

const BRIDGE = "http://127.0.0.1:18182";
// Where this repo is cloned. $HOME is expanded by bash (unquoted in the exec
// command, quoted in the log redirect), so no username is stored here.
// Change this ONE line if you clone elsewhere.
const SUBS_ROOT = "$HOME/AI/实时字幕";
// Persisted next to the bridge log so "what did it show / skip" can be reviewed.
const LOG_PATH = SUBS_ROOT + "/logs/plugin.log";
const START_SCRIPT = SUBS_ROOT + "/start_services.sh";

const CHUNK = 4.0;       // seconds of audio per request. Must exceed typical
                         // processing time so prefetch keeps up with 1x playback.
const LEAD = 12.0;       // look-ahead budget in wall-clock seconds, scaled by speed.
                         // mlx-whisper is ~1.0-1.6s per chunk steady state, but a
                         // chunk whose greedy decode degenerates (repetition loop)
                         // costs a bounded temperature retry of 2.5-7.5s. A 12s
                         // budget absorbs several of those in a row, so cues still
                         // arrive before the playhead reaches their window.
const TICK_MS = 250;
const SETTLE_MS = 600;   // grace period before deciding "this file has no subs"
                         // (embedded tracks are enumerated slightly after load)

let overlayReady = false;
let windowReady = false;
let active = false;
let servicesReady = false;
let starting = false;
let cues = [];
let nextTarget = 0;
let context = "";
let inFlight = false;
let lastPosition = -1;
let lastTickTime = 0;
let token = 0;
let currentVideo = "";
let shownText = "";
let ticks = 0;
let tickRunning = false;
let lastNotifiedSpeed = 1;

// console.log already goes to the IINA console; disk logging is batched.
let logBuf = [];
let lastLogFlush = 0;

// --- auto-activation state machine -------------------------------------------
// A file is only "decided" once its duration is known AND its subtitle tracks
// have had a moment to appear. The old code decided on the very first tick,
// where duration is still 0 and tracks are still empty, so it hit the
// "too short, skip" branch and then never looked at that file again -- which is
// exactly why opening a file in IINA did not start anything.
let pendingPath = "";     // file waiting for a subtitle decision
let pendingSince = 0;     // when we first noticed it
let probeAt = 0;          // start of the settle window before deciding
let decidedPath = "";     // path whose decision is final
let failStreak = 0;       // consecutive bridge failures (services may have died)
let lastRevive = 0;

function shellQuote(s) {
  return "'" + String(s).replace(/'/g, "'\\''") + "'";
}

function flushLog() {
  if (!logBuf.length) return;
  const batch = logBuf.splice(0);
  lastLogFlush = Date.now();
  const cmd = "printf '%s\\n' " + batch.map(shellQuote).join(" ") + ' >> "' + LOG_PATH + '"';
  try {
    const p = utils.exec("/bin/bash", ["-c", cmd]);
    if (p && typeof p.catch === "function") p.catch(() => {});
  } catch (e) {}
}

function log(msg) {
  const line = new Date().toISOString().slice(11, 23) + " " + msg;
  console.log("[LiveAI] " + msg);
  logBuf.push(line);
  if (logBuf.length >= 12 || Date.now() - lastLogFlush > 3000) flushLog();
}

function say(msg) { try { core.osd("[AI字幕] " + msg); } catch (e) {} }
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

function getSpeed() {
  try {
    const s = core.status.speed;
    return (typeof s === "number" && isFinite(s) && s > 0) ? s : 1;
  } catch (e) { return 1; }
}

// Bigger look-ahead at higher speed so generation still finishes before playback.
function leadFor(speed) {
  return Math.min(LEAD * Math.max(1, speed), 90);
}

function urlToPath(url) {
  if (!url) return "";
  if (url.indexOf("file://") === 0) {
    let p = decodeURIComponent(url.substring(7));
    if (p.indexOf("localhost/") === 0) p = p.substring(9);
    return p;
  }
  return url;
}

function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// A pure vocalisation line ("啊～"). Consecutive ones from neighbouring chunks
// are merged into one continuous cue instead of flickering on and off.
const INTERJECTION = /^(啊|呀|嗯|哈|呜|哦|诶|哎呀|咿|嘿|呼|唔|呃|哎|唉)$/;

// Reject Whisper repetition loops before they become the next prompt.
function isSaneJa(s) {
  if (!s || s.length > 60) return false;
  const core = String(s).replace(/[\s、。,.!?！？…]/g, "");
  if (core.length >= 12 && new Set(core).size / core.length < 0.35) return false;
  return true;
}

// Normalize subtitle text for dedup: strip punctuation/whitespace so
// "早上好" and "早上好。" compare equal.
function normText(s) {
  // ～/ー 是延长音记号，不参与去重比较，这样「啊～」和「啊」会被视为同一句。
  return String(s).replace(/[\s，。！？、,.!?…:：;；"'"'～~ー—]/g, "").toLowerCase();
}

// What is the subtitle situation right now?
//   visible = something is actually being shown.
// A file can carry subtitle tracks that IINA did not select (depends on the
// "auto-load subtitles" preference), in which case the viewer still sees no
// text -- that counts as "no subtitles" for our purpose, so we look at
// currentTrack / id, not just the track count.
function subtitleState() {
  let tracks = [];
  let id = null;
  let cur = null;
  try { tracks = core.subtitle.tracks || []; } catch (e) {}
  try { id = core.subtitle.id; } catch (e) {}
  try { cur = core.subtitle.currentTrack; } catch (e) {}
  const selected = (typeof id === "number" && id > 0) || !!cur;
  return { count: tracks.length, id: id, visible: tracks.length > 0 && selected };
}

function setupOverlay() {
  if (overlayReady || !windowReady) return;
  try {
    overlay.simpleMode();
    overlay.setStyle(
      '.sub { position:absolute; bottom:8%; left:50%; transform:translateX(-50%); ' +
      'max-width:92%; padding:5px 14px; background:rgba(0,0,0,0.55); color:#fff; ' +
      'font-size:26px; line-height:1.4; text-align:center; border-radius:6px; ' +
      'text-shadow:0 1px 3px rgba(0,0,0,0.9); font-family:"PingFang SC",sans-serif; }'
    );
    overlay.show();
    overlayReady = true;
    log("overlay ready");
  } catch (e) { log("overlay setup failed: " + e); }
}

async function checkBridge() {
  try {
    const r = await http.get(BRIDGE + "/health");
    const d = JSON.parse(r.text);
    return d.ok && d.whisper && d.llama;
  } catch (e) { return false; }
}

async function ensureServices() {
  if (await checkBridge()) { servicesReady = true; return true; }
  if (starting) return false;
  starting = true;
  say("正在启动本地识别服务…");
  log("exec start script");
  try {
    const r = await utils.exec("/bin/bash", ["-c", "exec " + START_SCRIPT]);
    log("start status=" + (r && r.status));
  } catch (e) { log("start error: " + e); }
  for (let i = 0; i < 140; i++) {
    if (await checkBridge()) {
      servicesReady = true; starting = false;
      say("识别服务已就绪"); log("services ready");
      return true;
    }
    await sleep(500);
  }
  starting = false;
  say("服务启动失败"); log("services FAILED");
  return false;
}

function deactivate(reason) {
  if (!active) return;
  active = false;
  token++;
  inFlight = false;
  cues = []; context = ""; shownText = "";
  if (overlayReady) overlay.setContent("");
  log("deactivate: " + reason);
}

async function activate(path) {
  currentVideo = path;
  log("activate " + path);
  const ok = await ensureServices();
  if (!ok) return;
  setupOverlay();
  active = true;
  cues = []; context = ""; shownText = "";
  const pos = core.status.position;
  nextTarget = Math.max(0, pos);   // start at playhead: first cue arrives ~1.8s later
  lastPosition = pos;
  lastTickTime = Date.now();
  token++;
  inFlight = false;
  if (overlayReady) overlay.setContent("");
  say("AI 字幕已开启");
  log("ACTIVE pos=" + pos.toFixed(1) + " dur=" + core.status.duration.toFixed(1) + " speed=" + getSpeed().toFixed(1));
}

async function prefetch(target, myToken) {
  if (inFlight || !active || myToken !== token) return;
  inFlight = true;
  try {
    const r = await http.post(BRIDGE + "/transcribe", {
      headers: { "Content-Type": "application/json" },
      data: { video: currentVideo, start: target, duration: CHUNK, context: context },
    });
    const d = JSON.parse(r.text);
    if (myToken !== token) return;
    failStreak = 0;   // bridge answered: services are alive
    if (d.ok && d.zh) {
      // Precise per-sentence cues from Whisper timestamps (absolute video time).
      // Fallback to the whole-chunk window if the bridge is an older version.
      const added = (d.cues && d.cues.length)
        ? d.cues.map((c) => ({ start: c.start, end: c.end, zh: c.zh }))
        : [];
      // Dedupe: short overlapping chunks can transcribe the same sentence twice
      // ("早上好" vs "早上好。"). Treat identical normalized text within 6s as one.
      for (const c of added) {
        const key = normText(c.zh);
        const dup = cues.find((x) =>
          normText(x.zh) === key && Math.abs(x.start - c.start) < 6.0);
        if (!dup) { cues.push(c); continue; }
        if (INTERJECTION.test(key) && c.start <= dup.end + 1.5) {
          dup.end = Math.max(dup.end, c.end);
        }
      }
      cues.sort((a, b) => a.start - b.start);
      // Only sane text may become the next chunk's prompt: a repetition loop
      // ("あらあらあら…") fed back in poisons every following chunk.
      if (d.ja && isSaneJa(d.ja)) context = d.ja.slice(-24);
      if (added.length) log("CUE @" + target.toFixed(1) + " segs=" + added.length +
          " span=" + added[0].start.toFixed(1) + "-" + added[added.length - 1].end.toFixed(1) +
          " zh=" + added.map((c) => c.zh).join(" | ").slice(0, 80));
    } else { log("transcribe fail: " + (d.error || "empty")); }
  } catch (e) {
    failStreak++;
    log("prefetch err(" + failStreak + "): " + e);
  } finally { if (myToken === token) inFlight = false; }
}

function renderAt(pos) {
  let text = "";
  for (const c of cues) { if (pos >= c.start && pos < c.end) { text = c.zh; break; } }
  if (cues.length > 60) cues = cues.filter((c) => c.end > pos - 5);
  if (text !== shownText) {
    if (overlayReady) overlay.setContent(text ? '<div class="sub">' + escapeHtml(text) + "</div>" : "");
    shownText = text;
    if (text) log("RENDER @" + pos.toFixed(1));
  }
}

async function tick() {
  ticks++;
  setupOverlay();

  const now = Date.now();
  const dt = lastTickTime > 0 ? (now - lastTickTime) / 1000 : TICK_MS / 1000;
  lastTickTime = now;
  const speed = getSpeed();

  if (core.status.idle) {
    if (active) deactivate("idle");
    // Clear everything so reopening the same file is decided again.
    currentVideo = "";
    pendingPath = ""; decidedPath = ""; probeAt = 0;
    failStreak = 0;
    lastPosition = -1;
    return;
  }

  const url = core.status.url || "";
  const path = urlToPath(url);
  const pos = core.status.position;
  const dur = core.status.duration;

  // New file: IINA auto-play, playlist advance, or a resumed session. Reset all
  // per-file state and leave the subtitle decision to the state machine below.
  if (path && path !== currentVideo) {
    currentVideo = path;
    decidedPath = "";
    pendingPath = path;
    pendingSince = now;
    probeAt = 0;
    active = false;
    inFlight = false;
    token++;
    cues = []; context = ""; shownText = "";
    if (overlayReady) overlay.setContent("");
    const st0 = subtitleState();
    log("new file=" + path + " subs=" + st0.count + " sel=" + st0.id +
        " dur=" + dur.toFixed(0) + " speed=" + speed.toFixed(1));
  }

  // Decide once, per file: wait until the duration is known and the track list
  // has had SETTLE_MS to settle (IINA scans embedded tracks slightly after
  // load; slow disks need more), then start AI subtitles only if nothing is
  // actually being displayed.
  if (!decidedPath && pendingPath && pendingPath === path && !active) {
    // A network stream has no local file to decode, so decide (skip) at once
    // instead of waiting for a duration that never arrives.
    const ready = core.status.isNetworkResource ||
                  (dur > 10 && (pos > 0.15 || now - pendingSince > 1500));
    if (ready || now - pendingSince > 12000) {
      if (!probeAt) {
        probeAt = now;
      } else if (now - probeAt >= SETTLE_MS) {
        const st = subtitleState();
        decidedPath = path;
        pendingPath = "";
        probeAt = 0;
        if (st.visible) {
          log("skip: subtitle showing (tracks=" + st.count + " id=" + st.id + ")");
        } else if (core.status.isNetworkResource) {
          log("skip: network resource (local ASR not applicable)");
        } else {
          log("auto: no subtitle showing (tracks=" + st.count + " id=" + st.id + ") -> activate");
          await activate(path);   // starts the local services when needed
          return;                 // lastPosition was just reset by activate()
        }
      }
    }
  }

  if (!active) return;

  // FIX: seek detection compares actual delta with expected travel (speed * wall time),
  // so small forward drags are caught and high-speed playback is not mistaken for a seek.
  const delta = pos - lastPosition;
  const expected = speed * dt;
  const tol = Math.max(0.7, expected * 0.6);
  if (lastPosition < 0 || Math.abs(delta - expected) > tol) {
    log("seek " + lastPosition.toFixed(1) + "->" + pos.toFixed(1) +
        " expected=" + expected.toFixed(2) + " delta=" + delta.toFixed(2));
    cues = []; context = ""; shownText = "";
    nextTarget = Math.max(0, pos);
    token++;
    inFlight = false;   // stale result is discarded by token check
    if (overlayReady) overlay.setContent("");
  }
  lastPosition = pos;

  renderAt(pos);

  if (speed > 3 && speed !== lastNotifiedSpeed) {
    say("高倍速下字幕可能延迟");
    lastNotifiedSpeed = speed;
  } else if (speed <= 3) {
    lastNotifiedSpeed = speed;
  }

  if (ticks % 40 === 0) {
    log("tick pos=" + pos.toFixed(0) + " cues=" + cues.length + " next=" + nextTarget.toFixed(0) +
        " speed=" + speed.toFixed(1) + " svc=" + servicesReady);
  }

  // Resync only when prefetch is FAR behind. ASR takes ~3s, so a modest lag is
  // normal and expected; aggressive resync (old threshold 0.5s) kept resetting
  // nextTarget to the playhead, so every cue arrived after its window passed.
  // Allow one LEAD worth of slack before giving up on a stale target.
  const lead = leadFor(speed);
  if (nextTarget < pos - lead * 0.8) {
    log("resync " + nextTarget.toFixed(1) + "->" + pos.toFixed(1));
    nextTarget = pos;
    context = "";
  }

  if (servicesReady && !inFlight && nextTarget < dur - CHUNK && nextTarget <= pos + lead + CHUNK) {
    const t = nextTarget;
    nextTarget += CHUNK;
    prefetch(t, token);   // fire-and-forget, serialized by inFlight
  }

  // User-loaded subtitle tracks take over from AI subtitles (and a track that
  // IINA selects after we started also stops us, so there is no double display).
  if (ticks % 8 === 0) {
    const st = subtitleState();
    if (st.visible && active) {
      say("检测到字幕轨，AI 字幕暂停");
      deactivate("subtitle track appeared");
    }
  }

  // The local services can die between films (sleep, logout, a killed process).
  // A few consecutive transport failures mean the bridge is gone: bring it back
  // without making the viewer do anything.
  if (failStreak >= 4 && now - lastRevive > 20000) {
    lastRevive = now;
    failStreak = 0;
    log("bridge unreachable -> reviving services");
    say("识别服务中断，正在重启…");
    ensureServices().then((ok) => { if (!ok) log("revive FAILED"); });
  }
}

event.on("iina.window-loaded", () => { log("evt window-loaded"); windowReady = true; setupOverlay(); });
event.on("iina.plugin-overlay-loaded", () => { log("evt overlay-loaded"); setupOverlay(); });

// FIX: guard against setInterval re-entry while an async tick is still running.
setInterval(() => {
  if (tickRunning) return;
  tickRunning = true;
  tick().catch((e) => log("tick exception: " + e)).finally(() => { tickRunning = false; });
}, TICK_MS);
log("plugin loaded");
