// Live AI Subtitles - IINA plugin
// Poll-driven so it works regardless of when file/overlay events fire.
const { console, core, event, overlay, http, utils, menu, subtitle } = iina;

const BRIDGE = "http://127.0.0.1:18182";
// Where this repo is cloned. Change this ONE line if you clone elsewhere.
//
// 注意：IINA 通过 utils.exec 起子进程时，环境里**没有 HOME**（2026-11-22 实测
// `env` 只剩 LC_ALL）。原来写 "$HOME/AI/实时字幕"，bash 会把 $HOME 展开成空串，
// 路径变成 /AI/实时字幕/... —— 于是启动脚本 exec 失败、日志写不进去，全都静默。
// 表现就是「字幕看着看着全没了、之后服务再也拉不起来、插件日志一片空白」。
// 改用 bash 的 ~ 展开（HOME 缺失时 bash 会回退查 passwd），配双引号兼容空格路径。
const SUBS_ROOT = "$(echo ~)/AI/实时字幕";
// Persisted next to the bridge log so "what did it show / skip" can be reviewed.
const LOG_PATH = SUBS_ROOT + "/logs/plugin.log";
const START_SCRIPT = SUBS_ROOT + "/start_services.sh";

const CHUNK = 4.0;       // seconds of audio per request. Must exceed typical
                         // processing time so prefetch keeps up with 1x playback.
// 暂停补全用的窗口。实测（同一片 2099/2103/2112/2116 四处对比）拉长到 6s 并
// 没有收益：耗时 2.9–3.5s（4s 窗也只要 2.9–3.4s，本机瓶颈是固定开销不是长度），
// 但 6s 更容易把重复拟声喂成退化（うんうんうんうん，degenerate=true）。
// 所以补全沿用播放窗口，靠"不停下"而不是"窗口更大"来提速。
const PAUSE_CHUNK = 4.0;
// 两次补全请求之间的最小间隔。0 = 能跑多快跑多快；留一点空隙是为了不在用户
// 暂停去干别的事时把 CPU 占满（服务端本来单飞，占空比已经很高）。
const PAUSE_GAP_MS = 200;
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
// overlay 当前是否处于 show 状态。IINA 的插件 overlay 是覆盖在主窗口内容区顶层的
// WKWebView（IINA 用全尺寸内容视图 + 自绘透明标题栏），常驻显示时可能把标题栏的
// 点击也吃掉，导致窗口拖不动。策略：**只在确实有字幕要显示时 show，字幕消失立刻
// hide**，把窗口拖动/点击还给 IINA。
let overlayShown = false;
let overlayHidden = false;   // 排查用：临时隐藏整层 overlay，验证它是否吞掉窗口拖动
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
let lastStartAttempt = 0;   // 上次尝试拉起服务的时间；失败后低频重试（见 tick）
// 本片是否已经决定"要 AI 字幕"。和 active 分开记：activate 里拉起服务失败时
// active 仍是 false，但仍应后台重试；而判定"已有字幕"跳过时它必须是 false，
// 否则会为了一个根本不需要字幕的影片反复拉起两个模型（白占 3.5GB 内存）。
let aiWanted = false;
// 被用户手动关闭 AI 字幕的影片路径。提前声明在这里，避免 tick 里引用时踩
// JavaScript 的暂时性死区（TDZ）——本项目踩过一次同类坑（见 AGENTS 坑 14）。
let userPaused = "";

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

// --- 暂停补全（scavenge）------------------------------------------------------
// 播放时只允许领先播放头 LEAD 秒，暂停后如果还这么克制，字幕文件永远跑不完。
// 暂停时改为"一个接一个往后啃"，把整片补进 <video>.live-ai.srt，目的是下次
// 打开（或本次继续看）直接复用，不再等 ASR。恢复播放/拖动立即让位。
let pauseFill = true;     // 菜单可关（省电、避免占 CPU）
let lastFillTick = 0;     // 上次发出补全请求的时间（节流用）
let lastFilling = false;  // 上一 tick 是否在补全（只用于日志边沿）
let fillDone = "";        // 已补到片尾的影片路径（避免重复提示）

function shellQuote(s) {
  return "'" + String(s).replace(/'/g, "'\\''") + "'";
}

// 12:34 或 1:02:03，用于把补全进度说给人听。
function fmtTime(t) {
  const s = Math.max(0, Math.floor(t || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  const p = (n) => (n < 10 ? "0" + n : "" + n);
  return (h > 0 ? h + ":" : "") + p(m) + ":" + p(ss);
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
  if (logBuf.length >= 30 || Date.now() - lastLogFlush > 8000) flushLog();
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

// 字幕符号体系统一（用户 2026-11-22 要求）：只允许 "..." "?" "~" 三种符号，
// 其它标点全部剔除；中文/假名/字母数字正常保留。
// bridge 侧已经做了同样的收口（normalize_punct），这里是显示层兜底：
// 旧缓存、旧 bridge 进程返回的文本也会被清洗，保证屏幕上不出现逗号句号问号以外的符号。
const PUNCT_PLACEHOLDER = "\u0001";
function cleanPunct(s) {
  let t = String(s == null ? "" : s);
  // 先把省略号占位，避免后面逐字符过滤把它的点当普通符号删掉
  t = t.replace(/…+/g, PUNCT_PLACEHOLDER);
  t = t.replace(/\.{2,}/g, PUNCT_PLACEHOLDER);
  t = t.replace(/？/g, "?").replace(/[～〜]/g, "~");
  let out = "";
  for (const ch of t) {
    if (ch === PUNCT_PLACEHOLDER) { out += ch; continue; }
    const c = ch.codePointAt(0);
    if (ch === " ") { out += ch; continue; }
    if ((c >= 0x4E00 && c <= 0x9FFF) || (c >= 0x3400 && c <= 0x4DBF)) { out += ch; continue; }
    if (c >= 0x3040 && c <= 0x30FF) { out += ch; continue; }
    if (c < 128 && /[A-Za-z0-9]/.test(ch)) { out += ch; continue; }
    if (ch === "?" || ch === "~") { out += ch; continue; }
    // 其余标点/符号丢弃
  }
  out = out.split(PUNCT_PLACEHOLDER).join("...").replace(/\s+/g, " ").trim();
  // 只剩符号没有文字的，视为空（调用方丢弃）
  if (!/[0-9A-Za-z\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF]/.test(out)) return "";
  return out;
}

// What is the subtitle situation right now?
//   visible = something is actually being shown.
// A file can carry subtitle tracks that IINA did not select (depends on the
// "auto-load subtitles" preference), in which case the viewer still sees no
// text -- that counts as "no subtitles" for our purpose, so we look at
// currentTrack / id, not just the track count.
//
// 2026-11-22：IINA 的自动加载是"字幕文件名含视频名"的模糊匹配，历史上会把我们
// 自己的缓存 <视频名>.live-ai.srt 当外挂字幕加载并选中（日志 title="live-ai.srt"），
// 插件因此误判"用户已有字幕"跳过 AI —— 缓存只覆盖看过的部分，后面就全空。
// 根治是缓存搬到 <视频目录>/.live-ai/ 子目录（bridge 侧已改），这里再加一道兜底：
// 标题里带 live-ai 的字幕轨一律当成"我们自己的缓存"，不算用户字幕。
function isOwnCacheTrack(t) {
  try { return /live-ai/i.test(String((t && t.title) || "")); } catch (e) { return false; }
}

function subtitleState() {
  let tracks = [];
  let id = null;
  let cur = null;
  try { tracks = core.subtitle.tracks || []; } catch (e) {}
  try { id = core.subtitle.id; } catch (e) {}
  try { cur = core.subtitle.currentTrack; } catch (e) {}
  // 用户自己的字幕轨（不含我们的缓存轨）才算数。
  const userTracks = tracks.filter((t) => !isOwnCacheTrack(t));
  const curOwn = isOwnCacheTrack(cur);
  const selected = !curOwn && ((typeof id === "number" && id > 0) || !!cur);
  return { count: userTracks.length, id: id, visible: userTracks.length > 0 && selected };
}

// 把字幕轨信息压缩成一行日志。排查「为什么插件判定已有字幕」时必须看这个：
// 2026-11-22 就是 IINA 按模糊匹配自动加载了 <视频名>.live-ai.srt，插件以为
// "用户已经有字幕"而跳过 AI，但那个文件只覆盖到 1254s，1600s 之后屏幕全空。
function trackInfo() {
  try {
    const ts = core.subtitle.tracks || [];
    return JSON.stringify(ts.map((t) => ({
      id: t.id, title: t.title, lang: t.language,
      sel: t.isSelected, ext: t.isExternal,
    }))).slice(0, 400);
  } catch (e) { return "?"; }
}

// 初始化字幕层。
//
// 关键：不能只等 `iina.window-loaded`。**插件热重载（IINA 设置里的 Reload）时窗口
// 早已存在，这个事件不会再触发**，windowReady 永远是 false → overlay 永远不初始化
// → 插件照常识别/渲染日志一切正常，但屏幕上一个字都没有（2026-11-22 用户报"字幕
// 没有显示"就是它：15:15 重载后 plugin.log 里只有 plugin loaded，没有 overlay ready）。
// 所以这里在收不到事件时也主动探测：每 2 秒试一次 simpleMode()，窗口没就绪会抛
// "window is not available"，吞掉等下次；成功一次就 lock 住 overlayReady。
let lastOverlayTry = 0;
function setupOverlay() {
  if (overlayReady) return;
  const now = Date.now();
  if (!windowReady && now - lastOverlayTry < 2000) return;   // 热重载探测节流
  lastOverlayTry = now;
  try {
    overlay.simpleMode();
    overlay.setStyle(
      '.sub { position:absolute; bottom:8%; left:50%; transform:translateX(-50%); ' +
      'max-width:92%; padding:5px 14px; background:rgba(0,0,0,0.55); color:#fff; ' +
      'font-size:26px; line-height:1.4; text-align:center; border-radius:6px; ' +
      'text-shadow:0 1px 3px rgba(0,0,0,0.9); font-family:"PingFang SC",sans-serif; }'
    );
    // 显式关掉 overlay 的鼠标交互。官方文档说默认就不接收，但实测 IINA 的 overlay
    // webview 仍可能吃掉标题栏区域的 mouseDown（表现为窗口拖不动），显式再设一次。
    try { overlay.setClickable(false); } catch (e) { log("setClickable failed: " + e); }
    // **不要在这里调 hide()**。IINA 文档写明 overlay 默认就是隐藏的（"The overlay
    // is hidden by default and you need to call show()"），而初始化阶段就 hide 会让
    // 之后的第一次 show() 不渲染内容 —— 用户实测：Reload 后第一段字幕不出，手动
    // 切一次「AI 字幕：开/关」（触发第二次 show）才恢复正常。只靠 overlaySet 控制
    // 显隐：有字幕才 show，没字幕才 hide。
    overlayReady = true;
    windowReady = true;
    overlayShown = false;
    log("overlay ready (default hidden, shows on first cue)");
  } catch (e) {
    // 首次打开文件时窗口可能真的还没准备好：保持 windowReady=false，2 秒后再试。
    if (windowReady) log("overlay setup failed: " + e);
  }
}

// 统一出口：有字幕才 show + 填内容；没有字幕就清空并 hide（延迟 800ms，避免
// 句子间隙里 overlay 反复 show/hide 造成闪烁）。
// 这样既能让字幕照常显示，又把绝大多数时间（句间停顿、无对白段）的鼠标事件
// 还给 IINA —— 包括拖标题栏。
let overlayHideTimer = 0;
function overlayHideNow() {
  if (overlayHideTimer) { clearTimeout(overlayHideTimer); overlayHideTimer = 0; }
  try { if (overlayShown) { overlay.setContent(""); overlay.hide(); overlayShown = false; } } catch (e) {}
}
function overlaySet(text) {
  if (!overlayReady) return;
  if (overlayHidden) {   // 排查开关打开时整层都不显示
    overlayHideNow();
    return;
  }
  try {
    if (text) {
      if (overlayHideTimer) { clearTimeout(overlayHideTimer); overlayHideTimer = 0; }
      // 顺序很重要：**先 show 再 setContent**。IINA 的 overlay 在隐藏状态下有时
      // 会把 setContent 吞掉（2026-11-22 实测：Reload 后第一段字幕不显示，切换一次
      // 开关触发第二次 show 才出来）。先 show 拿到可见的 webview，再填内容，同帧完成。
      if (!overlayShown) { overlay.show(); overlayShown = true; }
      overlay.setContent('<div class="sub">' + escapeHtml(text) + "</div>");
    } else if (overlayShown && !overlayHideTimer) {
      overlayHideTimer = setTimeout(() => {
        overlayHideTimer = 0;
        try { overlay.setContent(""); overlay.hide(); overlayShown = false; } catch (e) {}
      }, 800);
    }
  } catch (e) { log("overlay set failed: " + e); }
}

async function checkBridge() {
  try {
    const r = await http.get(BRIDGE + "/health");
    const d = JSON.parse(r.text);
    return d.ok && d.whisper && d.llama;
  } catch (e) { return false; }
}

// 扫视频同目录，找**用户自带的外挂字幕文件**（需求：有用户字幕就完全不用 AI 字幕）。
//
// 为什么要主动扫、而不是只看 IINA 有没有加载：IINA 的自动加载依赖偏好设置，用户
// 也可能手动加载；但"目录里有与影片同名的 .srt/.ass/.vtt" 这件事本身足以说明用户
// 手里有字幕，这时再去跑 ASR/翻译就是白烧 3.5GB 内存。
//
// 我们自己的 AI 产物必须排除，否则会被误判成"用户字幕"：
//   <视频>.live-ai.srt / .live-ai.ranges   旧版缓存命名（会被 bridge 迁进 .live-ai/）
//   <视频>.zh.srt                           /subtitle/finalize 导出的成品（仍是 AI 生成）
//   <视频>.zh.srt.part                      旧版边写边存的半成品
// 另外 .live-ai/ 子目录里的缓存本身不会命中（只 ls 视频目录这一层）。
async function scanUserSubs(path) {
  try {
    const slash = path.lastIndexOf("/");
    if (slash <= 0) return [];
    const dir = path.slice(0, slash);
    const base = path.slice(slash + 1).replace(/\.[^.\/]+$/, "");
    if (!base) return [];
    // 用 $1 传参，避免目录名里的引号/空格把命令拆坏
    const r = await utils.exec("/bin/bash", ["-c",
      'ls -1 "$1" 2>/dev/null || true', "bash", dir]);
    const all = String((r && (r.stdout || r.output)) || "").split("\n")
      .map((s) => s.trim()).filter(Boolean);
    // 只认 "<视频名>.<任意>.srt|ass|ssa|vtt|sub"（点分隔），这样
    //   film.srt / film.chs.srt / film.zh-CN.ass   ✓
    //   film-trailer.srt（别的片子/预告）           ✗
    const pref = base.toLowerCase() + ".";
    return all.filter((n) => {
      const low = n.toLowerCase();
      if (!low.startsWith(pref)) return false;
      if (!/\.(srt|ass|ssa|vtt|sub)$/.test(low)) return false;
      if (low.indexOf(".live-ai.") >= 0) return false;                   // 旧版 AI 缓存
      if (low.endsWith(".zh.srt") || low.endsWith(".part")) return false; // AI 导出/半成品
      return true;
    });
  } catch (e) {
    log("scan user subs failed: " + e);
    return [];
  }
}

// 画面里是否已经有烧进去的字幕（硬字幕）。失败一律当 false（宁可多开字幕）。
async function hasBurnedInSubs(path, pos) {
  try {
    const url = BRIDGE + "/hardsub?video=" + encodeURIComponent(path) +
                "&start=" + Math.max(0, pos).toFixed(1);
    const r = await http.get(url);
    const d = JSON.parse(r.text);
    if (d && d.ok) log("hardsub check: " + (d.hardsub ? "detected" : "none"));
    return !!(d && d.hardsub);
  } catch (e) {
    log("hardsub check failed: " + e);
    return false;
  }
}

// 字幕缓存的状态。缓存是边看边累加的，所以"有文件"不等于"整片都有字幕"——
// 它只是让已有的部分秒回，缺的部分照常识别再补写回去。
async function subtitleCacheStatus(path, dur) {
  try {
    const url = BRIDGE + "/subtitle?video=" + encodeURIComponent(path) +
                "&duration=" + (dur > 0 ? dur.toFixed(1) : "0");
    const r = await http.get(url);
    const d = JSON.parse(r.text);
    if (d && d.ok && d.cues > 0) {
      const pct = (d.coverage != null) ? Math.round(d.coverage * 100) : null;
      log("subtitle cache: " + d.cues + " cues" +
          (pct != null ? (", " + pct + "% covered") : "") +
          (d.gaps && d.gaps.length ? ", " + d.gaps.length + " gap(s)" : ""));
      return d;
    }
    return null;
  } catch (e) {
    log("subtitle cache check failed: " + e);
    return null;
  }
}

// 把缓存导出成标准命名的 .zh.srt（缓存本身一直在写，这个只是给手动加载用）。
async function exportSubtitles(path, dur) {
  if (!path) return false;
  try {
    const r = await http.post(BRIDGE + "/subtitle/finalize", {
      headers: { "Content-Type": "application/json" },
      data: { video: path, duration: dur > 0 ? dur : 0 },
    });
    const d = JSON.parse(r.text);
    if (d && d.ok) {
      log("exported subtitle: " + d.path + " cues=" + d.cues);
      say("已导出为标准字幕：" + d.path);
      return true;
    }
    log("export skipped: " + ((d && d.error) || "?"));
    return false;
  } catch (e) {
    log("export failed: " + e);
    return false;
  }
}

// quiet=true 用于"后台自动重试"：不弹 OSD，避免每 30s 骚扰用户。
async function ensureServices(quiet) {
  if (await checkBridge()) { servicesReady = true; return true; }
  if (starting) return false;
  starting = true;
  if (!quiet) say("正在启动本地识别服务…");
  // 注意这里给脚本路径加了双引号：START_SCRIPT 内含 $(echo ~)，
  // 不加引号时命令替换结果会被 bash 分词，路径里的空格会把参数拆开。
  const startCmd = 'exec "' + START_SCRIPT + '"';
  log("exec start script: " + startCmd);
  try {
    const r = await utils.exec("/bin/bash", ["-c", startCmd]);
    log("start status=" + (r && r.status));
  } catch (e) { log("start error: " + e); }
  for (let i = 0; i < 140; i++) {
    if (await checkBridge()) {
      servicesReady = true; starting = false;
      if (!quiet) say("识别服务已就绪");
      log("services ready");
      return true;
    }
    await sleep(500);
  }
  starting = false;
  if (!quiet) say("服务启动失败");
  log("services FAILED");
  return false;
}

function deactivate(reason) {
  if (!active) return;
  active = false;
  token++;
  inFlight = false;
  cues = []; context = ""; shownText = "";
  overlaySet("");
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
  overlaySet("");
  say("AI 字幕已开启");
  log("ACTIVE pos=" + pos.toFixed(1) + " dur=" + core.status.duration.toFixed(1) + " speed=" + getSpeed().toFixed(1));
}

async function prefetch(target, myToken, chunk) {
  if (inFlight || !active || myToken !== token) return;
  const dur = chunk || CHUNK;
  inFlight = true;
  try {
    const r = await http.post(BRIDGE + "/transcribe", {
      headers: { "Content-Type": "application/json" },
      data: { video: currentVideo, start: target, duration: dur, context: context, save: true },
    });
    const d = JSON.parse(r.text);
    if (myToken !== token) return;
    failStreak = 0;   // bridge answered: services are alive
    if (d.ok && d.zh) {
      // Precise per-sentence cues from Whisper timestamps (absolute video time).
      // Fallback to the whole-chunk window if the bridge is an older version.
      const added = (d.cues && d.cues.length)
        ? d.cues.map((c) => ({ start: c.start, end: c.end, zh: cleanPunct(c.zh) }))
            .filter((c) => c.zh)   // 清洗后只剩符号的条目直接丢
        : [];
      // Dedupe: short overlapping chunks can transcribe the same sentence twice
      // ("早上好" vs "早上好。"). Treat identical normalized text within 6s as one.
      //
      // 2026-11-22 改：以前只有拟声（啊～）会把时间延长，普通句子重复时直接丢弃新
      // 的那条。但窗口边界处的一句话常常被两个窗口各识别一次（前一条被截在窗口末、
      // 后一条从窗口头开始），直接丢弃会让它缺半句。现在统一取**时间并集**：
      // 同文本的相邻条目合并成一条，再把重叠部分交给下面的全局整理裁掉。
      for (const c of added) {
        const key = normText(c.zh);
        const dup = cues.find((x) =>
          normText(x.zh) === key && Math.abs(x.start - c.start) < 6.0);
        if (!dup) { cues.push(c); continue; }
        dup.start = Math.min(dup.start, c.start);
        dup.end = Math.max(dup.end, c.end);
      }
      cues.sort((a, b) => a.start - b.start);
      // 全局时间轴整理：后一条不早于前一条结束，避免两条 cue 同时命中
      // （bridge 也会整理写盘文件，这里是显示层的保险）。
      for (let i = 1; i < cues.length; i++) {
        const p = cues[i - 1], c = cues[i];
        if (c.start >= p.end - 0.01) continue;
        if (c.start - p.start >= 0.4) {
          p.end = c.start;
        } else {
          const len = Math.max(c.end - c.start, 0.4);
          c.start = p.end;
          c.end = c.start + len;
        }
      }
      // 复用自字幕文件的内容不是这一块现听的，不能当下一块的 prompt。
      if (d.reset_context) context = "";
      // Only sane text may become the next chunk's prompt: a repetition loop
      // ("あらあらあら…") fed back in poisons every following chunk.
      if (d.ja && isSaneJa(d.ja)) context = d.ja.slice(-24);
      if (d.from_srt) log("REUSE @" + target.toFixed(1) + " (from subtitle file)");
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
  // 显示前兜底清洗符号：旧缓存/旧 bridge 返回的文本也不会漏出多余标点
  for (const c of cues) { if (pos >= c.start && pos < c.end) { text = cleanPunct(c.zh); break; } }
  if (cues.length > 60) cues = cues.filter((c) => c.end > pos - 5);
  if (text !== shownText) {
    overlaySet(text);
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
    aiWanted = false;
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
    fillDone = ""; lastFilling = false;
    aiWanted = false;
    overlaySet("");
    const st0 = subtitleState();
    log("new file=" + path + " subs=" + st0.count + " sel=" + st0.id +
        " dur=" + dur.toFixed(0) + " speed=" + speed.toFixed(1) +
        " tracks=" + trackInfo());
  }

  // 服务自愈（2026-11-22 修）：ensureServices 只在 activate 时调用一次，失败后
  // 没有任何重试；如果服务恰好在两次播放之间挂掉（swap/手动 kill/启动脚本路径
  // 出错），插件就会一直空屏，直到重载插件才恢复。这里补一个 30s 低频重试：
  // 只在本片确实需要 AI 字幕、且用户没有手动关闭时才拉服务，避免白占内存。
  if (aiWanted && !servicesReady && !starting && currentVideo &&
      userPaused !== currentVideo && now - lastStartAttempt > 30000) {
    lastStartAttempt = now;
    log("services not ready -> background retry");
    ensureServices(true).then((ok) => { if (!ok) log("background retry FAILED"); });
  }

  // Decide once, per file: wait until the duration is known and the track list
  // has had SETTLE_MS to settle (IINA scans embedded tracks slightly after
  // load; slow disks need more), then start AI subtitles only if nothing is
  // actually being displayed.
  if (!decidedPath && pendingPath && pendingPath === path && !active && userPaused !== path) {
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
          // 这里必须把字幕轨明细打出来：曾有「判定已有字幕就跳过」的情况，实际是
          // IINA 按文件名模糊匹配自动加载了 <视频名>.live-ai.srt（我们自己的缓存），
          // 而该文件只覆盖到 1254s，用户看到 1600s 之后全空。
          log("skip: subtitle showing (tracks=" + st.count + " id=" + st.id +
              " tracks=" + trackInfo() + ")");
        } else if (core.status.isNetworkResource) {
          log("skip: network resource (local ASR not applicable)");
        } else {
          // 规则（2026-11-22 用户要求）：先看有没有**用户自带**的外挂字幕文件。
          // 有 → 完全不用 AI 字幕（即便 IINA 这次没自动加载它，也说明用户手里有）。
          const usersubs = await scanUserSubs(path);
          if (usersubs.length) {
            log("skip: user subtitle file(s) present: " + usersubs.join(", "));
            say("检测到外挂字幕文件 " + usersubs[0] + "，不启用 AI 字幕");
            return;
          }
          // 没有字幕轨，但画面里可能有"烧进去"的字幕（这些片源很常见）。
          // 只截底部字幕带做 OCR，命中就不再叠加 AI 字幕。
          const burned = await hasBurnedInSubs(path, pos);
          if (burned) {
            log("skip: burned-in subtitle detected in picture");
            say("画面已有硬字幕，跳过 AI 字幕");
            return;
          }
          // 没有用户字幕 → 走 AI 这一套：有缓存就从缓存秒回（bridge 每块先查
          // <视频目录>/.live-ai/ 里的字幕文件），缺的边看边补；暂停时后台补全。
          const cache = await subtitleCacheStatus(path, dur);
          if (cache) {
            const pct = (cache.coverage != null) ? Math.round(cache.coverage * 100) : 0;
            say("已有 AI 字幕缓存（覆盖 " + pct + "%），缺失部分实时补齐");
          }
          log("auto: no subtitle showing (tracks=" + st.count + " id=" + st.id + ") -> activate");
          aiWanted = true;        // 先置位：activate 里拉服务失败时 tick 会后台重试
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
    overlaySet("");
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
        " speed=" + speed.toFixed(1) + " svc=" + servicesReady +
        (core.status.paused === true ? " paused" : "") + (pauseFill ? "" : " fill=off"));
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

  // 暂停补全：暂停后不再受 LEAD 预算约束，一个窗口接一个窗口往后啃，
  // 把整片写进 <video>.live-ai.srt。恢复播放/拖动时 filling 立刻变 false，
  // 通道马上让回给实时请求（最多等当前那一个窗口跑完）。
  const paused = core.status.paused === true;
  const filling = paused && pauseFill && !!currentVideo;
  const wantLen = filling ? PAUSE_CHUNK : CHUNK;
  const tailLeft = dur > 0 ? dur - nextTarget : 0;
  const withinBudget = filling || nextTarget <= pos + lead + CHUNK;
  const gapOK = !filling || (now - lastFillTick >= PAUSE_GAP_MS);

  if (filling !== lastFilling) {
    log((filling ? "pause-fill start @" : "pause-fill stop @") + nextTarget.toFixed(1) +
        " pos=" + pos.toFixed(1) + " dur=" + dur.toFixed(0));
    lastFilling = filling;
  }

  if (servicesReady && !inFlight && tailLeft > 1.5 && withinBudget && gapOK) {
    const t = nextTarget;
    const len = Math.min(wantLen, tailLeft);
    nextTarget += len;
    if (filling) lastFillTick = now;
    prefetch(t, token, len);   // fire-and-forget, serialized by inFlight
  } else if (filling && !inFlight && dur > 10 && tailLeft <= 1.5 &&
             fillDone !== currentVideo) {
    fillDone = currentVideo;
    log("pause-fill complete cues=" + cues.length + " next=" + nextTarget.toFixed(0));
    say("本片字幕已补到片尾（" + cues.length + " 条），已写入字幕文件");
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
// 日志落盘兜底：log() 只在"有下一条日志进来"时检查 8s 阈值，如果插件卡在某个
// await 上就没有下一条日志，内存里那几条（往往正是卡住前最后的状态）永远不落盘，
// 排查时只能看到很早以前的内容。定时 flush 保证最坏 5s 内写盘。
setInterval(flushLog, 5000);
// --- 插件菜单：IINA 设置里的开关只在重载时生效，这里给一个立即生效的开关 ---

function toggleSubtitles() {
  if (active) {
    userPaused = currentVideo;
    aiWanted = false;
    deactivate("paused by user");
    say("AI 字幕已暂停");
  } else if (currentVideo) {
    userPaused = "";
    decidedPath = currentVideo;
    aiWanted = true;
    activate(currentVideo);
  } else {
    say("当前没有播放中的影片");
  }
}

try {
  menu.addItem(menu.item("AI 字幕：开 / 关", () => toggleSubtitles()));
  menu.addItem(menu.item("AI 字幕：本片已有硬字幕（本片不再启用）", () => {
    const p = currentVideo;
    if (!p) return;
    userPaused = p;
    decidedPath = p;
    aiWanted = false;
    deactivate("marked as hardsub");
    say("已标记本片有硬字幕，本片不再启用 AI 字幕");
  }));
  menu.addItem(menu.item("AI 字幕：字幕缓存状态", () => {
    const p = currentVideo;
    if (!p) { say("当前没有播放中的影片"); return; }
    const dur = core.status.duration || 0;
    http.get(BRIDGE + "/subtitle?video=" + encodeURIComponent(p) +
             "&duration=" + (dur > 0 ? dur.toFixed(1) : "0")).then((r) => {
      const d = JSON.parse(r.text);
      if (!d || !d.ok) { say("没有字幕缓存"); return; }
      const pct = (d.coverage != null) ? Math.round(d.coverage * 100) : 0;
      say("缓存 " + d.cues + " 条，覆盖 " + pct + "%，未覆盖 " +
          ((d.gaps || []).length) + " 段");
    }).catch(() => say("服务未响应"));
  }));
  menu.addItem(menu.item("AI 字幕：暂停时后台补全（开 / 关）", () => {
    pauseFill = !pauseFill;
    lastFilling = false;
    log("pause-fill " + (pauseFill ? "enabled" : "disabled"));
    say("暂停时后台补全：" + (pauseFill ? "开（暂停后继续往后识别）" : "关"));
  }));
  menu.addItem(menu.item("AI 字幕：补全进度", () => {
    const d = core.status.duration || 0;
    if (!currentVideo || !(d > 0)) { say("当前没有播放中的影片"); return; }
    const pct = Math.min(100, Math.round((nextTarget / d) * 100));
    say("已补到 " + fmtTime(nextTarget) + " / " + fmtTime(d) +
        "（" + pct + "%）" + (pauseFill ? "，暂停后会自动继续" : "，补全已关闭"));
  }));
  menu.addItem(menu.item("AI 字幕：临时隐藏字幕层（排查窗口拖动）", () => {
    overlayHidden = !overlayHidden;
    // 统一走 overlaySet：它内部按 overlayHidden 决定是否显示，不需要直接调 hide/show
    overlaySet(overlayHidden ? "" : shownText);
    log("overlay " + (overlayHidden ? "hidden" : "normal") + " (window-drag test)");
    say(overlayHidden ? "字幕层已隐藏，现在试着拖窗口" : "字幕层已恢复（有字幕时才显示）");
  }));
  menu.addItem(menu.item("AI 字幕：导出为标准字幕文件", () => {
    const p = currentVideo;
    if (!p) { say("当前没有播放中的影片"); return; }
    exportSubtitles(p, core.status.duration || 0);
  }));
  menu.addItem(menu.item("AI 字幕：服务状态", () => {
    http.get(BRIDGE + "/health").then((r) => {
      try { say("服务: " + r.text.replace(/[{}"]/g, "")); } catch (e) {}
    }).catch(() => say("服务未响应"));
  }));
} catch (e) { log("menu setup failed: " + e); }

log("plugin loaded");
