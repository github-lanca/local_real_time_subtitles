// Smoke-test the plugin's auto-activation state machine outside IINA.
// Loads the real main.js with a fake `iina` global, drives ticks manually and
// asserts what the plugin decided.
const fs = require("fs");
const vm = require("vm");
const path = require("path");

const SRC = path.join(__dirname, "..", "live-ai-subs", "main.js");

function makeEnv(scenario) {
  const calls = { osd: [], setContent: [], exec: [], post: [], logs: [], menu: [], loadTrack: [], finalize: [], healthLeft: scenario.healthDown || 0 };
  const status = Object.assign(
    { idle: false, url: "file:///tmp/movie.mp4", position: 0, duration: 0, speed: 1, isNetworkResource: false },
    scenario.status || {}
  );
  const subtitle = Object.assign({ tracks: [], id: -1, currentTrack: null }, scenario.subtitle || {});
  const env = {
    console: { log: (m) => calls.logs.push(String(m)) },
    setTimeout, setInterval: () => 0, clearInterval: () => {},
    Date, JSON, Math, String, Number, Promise, decodeURIComponent, isFinite, Object, Array,
    iina: {
      core: {
        status,
        subtitle,
        osd: (m) => calls.osd.push(m),
      },
      console: { log: (m) => calls.logs.push(String(m)), warn: () => {}, error: () => {} },
      event: { on: () => 0 },
      overlay: {
        simpleMode: () => {}, setStyle: () => {}, show: () => {},
        hide: () => {}, setClickable: () => {},
        setContent: (h) => calls.setContent.push(h),
      },
      http: {
        get: async (url) => {
          if (String(url).indexOf("/subtitle?") >= 0) {
            return { text: JSON.stringify({
              ok: true,
              path: "/tmp/film.live-ai.srt",
              exists: !!scenario.savedSub,
              cues: scenario.savedSub ? 120 : 0,
              coverage: scenario.savedSub ? 0.42 : 0,
              gaps: scenario.savedSub ? [[1800, 7200]] : [],
            }) };
          }
          if (String(url).indexOf("/hardsub") >= 0) {
            calls.hardsubQueries = (calls.hardsubQueries || 0) + 1;
            return { text: JSON.stringify({ ok: true, hardsub: !!scenario.hardsub, samples: [] }) };
          }
          if (scenario.healthDown && calls.healthLeft-- > 0) throw new Error("ECONNREFUSED");
          return { text: JSON.stringify({ ok: true, whisper: true, llama: true }) };
        },
        post: async (url, opts) => {
          if (String(url).indexOf("/subtitle/finalize") >= 0) {
            calls.finalize.push(opts.data);
            return { text: JSON.stringify({ ok: true, path: "/tmp/film.zh.srt", cues: 120, coverage: 0.99 }) };
          }
          calls.post.push(opts.data);
          return { text: JSON.stringify({ ok: true, ja: "テスト", zh: "测试", cues: [{ start: opts.data.start + 0.5, end: opts.data.start + 1.5, zh: "测试" }] }) };
        },
      },
      subtitle: { loadTrack: (p) => calls.loadTrack.push(p) },
      utils: { exec: (file, args) => {
        calls.exec.push([file].concat(args || []));
        // scanUserSubs 用 ls -1 列视频目录；按场景返回假的文件列表
        if (scenario.dirFiles && args && String(args[1] || "").indexOf("ls -1") >= 0) {
          return { status: 0, stdout: scenario.dirFiles.join("\n") };
        }
        return { status: 0 };
      } },
      menu: {
        item: (title, action, opts) => ({ title, action, opts }),
        addItem: (it) => calls.menu.push(it.title),
        separator: () => ({ title: "---" }),
      },
    },
  };
  return { env, calls, status, subtitle };
}

// Replace the trailing setInterval(...) with an exported tick so we can drive it.
function loadTick() {
  let src = fs.readFileSync(SRC, "utf8");
  src = src.replace(/setInterval\(\(\) => \{/, "globalThis.__tickWrap = (() => {");
  src = src.replace(/\}, TICK_MS\);/, "});");
  src += "\nglobalThis.__runTick = () => { globalThis.__n = (globalThis.__n||0)+1; __tickWrap(); };";
  src += "\nglobalThis.__dbg = () => ({ active: active, next: nextTarget });";
  return src;
}

async function run(scenario, ticks, label, delay) {
  const { env, calls, status, subtitle } = makeEnv(scenario);
  env.globalThis = env;
  const ctx = vm.createContext(env);
  vm.runInContext(loadTick(), ctx, { filename: "main.js" });

  for (let i = 0; i < ticks.length; i++) {
    Object.assign(status, ticks[i].status || {});
    if (ticks[i].subtitle) Object.assign(subtitle, ticks[i].subtitle);
    // tick is async-and-fire-forget; give its awaits room to settle.
    vm.runInContext("__runTick()", ctx);
    // Real ticks are 250ms apart; the settle window is 450ms, so ~2 ticks.
    await new Promise((r) => setTimeout(r, delay || 330));
  }

  // 服务已在运行时不重复 exec 启动脚本；用 ACTIVE 日志 + 实际请求判定"已接管"
  // exec 现在的形态是 ["/bin/bash", "-c", "exec $HOME/AI/实时字幕/start_services.sh"]
  const started = calls.exec.some((c) => c.join(" ").includes("start_services.sh"));
  const active = calls.logs.some((l) => /ACTIVE/.test(l)) || calls.post.length > 0;
  console.log("\n=== " + label + " ===");
  console.log("  日志: " + calls.logs.filter((l) => /auto:|skip:|new file|ACTIVE/.test(l)).join(" | "));
  console.log("  启动服务: " + started + "   AI 字幕激活: " + active + "   请求数: " + calls.post.length);
  if (calls.menu.length) console.log("  菜单项: " + calls.menu.join(" | "));
  if (calls.loadTrack.length) console.log("  加载字幕轨: " + calls.loadTrack.join(", "));
  return { started, active, calls };
}

(async () => {
  // A) 典型自动播放：首帧 duration=0/无字幕轨，随后 duration 到位 -> 应启动
  const a = await run({}, [
    {},
    { status: { duration: 0 } },
    { status: { duration: 5400, position: 0.3 } },
    { status: { position: 0.6 } },
    { status: { position: 1.0 } },
    { status: { position: 1.4 } },
    { status: { position: 1.8 } },
  ], "A 无字幕 + 自动播放");

  // B) 内嵌字幕且已被选中 -> 不应启动
  const b = await run({ subtitle: { tracks: [{ id: 1, title: "JP" }], id: 1, currentTrack: { id: 1 } } }, [
    {},
    { status: { duration: 5400, position: 0.3 } },
    { status: { position: 0.6 } },
    { status: { position: 1.0 } },
    { status: { position: 1.4 } },
  ], "B 有字幕且已选中");

  // C) 有字幕轨但 IINA 没选中（观众看不到字）-> 应启动
  const c = await run({ subtitle: { tracks: [{ id: 1, title: "JP" }], id: -1, currentTrack: null } }, [
    {},
    { status: { duration: 5400, position: 0.3 } },
    { status: { position: 0.6 } },
    { status: { position: 1.0 } },
    { status: { position: 1.4 } },
  ], "C 有轨但未选中");

  // D) 网络串流 -> 不启动
  const d = await run({ status: { isNetworkResource: true } }, [
    {},
    { status: { duration: 0, position: 1 } },
    { status: { position: 2 } },
    { status: { position: 3 } },
  ], "D 网络资源");

  // E) 服务没在跑 -> 必须自动执行 start_services.sh 再接管
  const e = await run({ healthDown: 3 }, [
    {},
    { status: { duration: 5400, position: 0.3 } },
    { status: { position: 0.6 } },
    { status: { position: 1.0 } },
    { status: { position: 1.4 } },
    { status: { position: 1.8 } },
    { status: { position: 2.2 } },
    { status: { position: 2.6 } },
    { status: { position: 3.0 } },
    { status: { position: 3.4 } },
    { status: { position: 3.8 } },
  ], "E 服务未运行 -> 自动拉起");

  // F) 画面已有硬字幕 -> 不应启动 AI 字幕
  const f = await run({ hardsub: true }, [
    {},
    { status: { duration: 5400, position: 0.3 } },
    { status: { position: 0.6 } },
    { status: { position: 1.0 } },
    { status: { position: 1.4 } },
  ], "F 画面有硬字幕");

  // G) 字幕缓存只覆盖了一部分 -> 仍然要接管，缺失区间靠管线补齐
  const g = await run({ savedSub: true }, [
    {},
    { status: { duration: 7200, position: 0.3 } },
    { status: { position: 0.6 } },
    { status: { position: 1.0 } },
    { status: { position: 1.4 } },
    { status: { position: 1.8 } },
  ], "G 有部分字幕缓存 -> 仍接管补齐");

  // H) 暂停 -> 应越过 LEAD 预算持续往后补全（目的：暂停时把整片字幕跑出来）
  const pausedTicks = [];
  for (let i = 0; i < 12; i++) pausedTicks.push({ status: { position: 100, paused: true } });
  const h = await run({}, [
    {},
    { status: { duration: 5400, position: 100 } },
    { status: { position: 100 } },
    { status: { position: 100 } },
  ].concat(pausedTicks), "H 暂停 -> 后台补全");

  // I) 补全途中恢复播放 -> 必须立刻停止往后啃（把通道让回实时）
  const hc = h.calls.post.length;
  const iRes = await run({}, [
    {},
    { status: { duration: 5400, position: 100 } },
    { status: { position: 100 } },
    { status: { position: 100 } },
  ].concat(pausedTicks.slice(0, 6)).concat([
    { status: { position: 100, paused: true } },
    { status: { position: 100, paused: false } },
    { status: { position: 100, paused: false } },
    { status: { position: 100, paused: false } },
  ]), "I 恢复播放 -> 补全让位");

  // J) 目录里有用户自带的外挂字幕文件（movie.srt）-> 完全不用 AI 字幕
  //    规则来源（用户 2026-11-22）：先看有没有用户字幕文件，有就不用插件。
  const j = await run({ dirFiles: ["movie.mp4", "movie.srt"] }, [
    {},
    { status: { duration: 5400, position: 0.3 } },
    { status: { position: 0.6 } },
    { status: { position: 1.0 } },
    { status: { position: 1.4 } },
  ], "J 用户自带外挂字幕 -> 不接管");

  // K) 目录里只有我们自己的 AI 产物（旧缓存/导出成品）-> 仍要接管、继续补齐
  const k = await run({ dirFiles: ["movie.mp4", "movie.live-ai.srt", "movie.zh.srt", "movie.zh.srt.part"] }, [
    {},
    { status: { duration: 5400, position: 0.3 } },
    { status: { position: 0.6 } },
    { status: { position: 1.0 } },
    { status: { position: 1.4 } },
    { status: { position: 1.8 } },
  ], "K 只有 AI 字幕产物 -> 仍接管");

  // G 的关键：有缓存也必须接管（缓存可能只覆盖某几段），且不去改字幕轨
  const maxEnd = (calls) => calls.post.reduce((m, d) => Math.max(m, d.start + d.duration), 0);
  const hEnd = maxEnd(h.calls);
  const iEnd = maxEnd(iRes.calls);
  const durations = Array.from(new Set(h.calls.post.map((d) => d.duration)));
  console.log("  补全最远到: " + hEnd.toFixed(0) + "s（LEAD 预算只有 " + (100 + 12 + 4) +
              "s）；窗口=" + JSON.stringify(durations) + "；请求数 " + hc);
  console.log("  恢复播放后最远: " + iEnd.toFixed(0) + "s；请求数 " + iRes.calls.post.length);
  const fillOK = hEnd > 140 && durations.length === 1 && durations[0] === 4 && hc > 8;
  // 6 个暂停 tick 最多推到 124；恢复播放的 4 个 tick 不得再往前推进
  const yieldOK = iEnd <= 128 && iRes.calls.post.length <= 7;

  const ok = a.active && !b.active && c.active && !d.active && e.started && e.active &&
             !f.active && g.active && g.calls.loadTrack.length === 0 &&
             g.calls.post.length > 0 && fillOK && yieldOK &&
             !j.active && k.active;
  if (!ok) {
    console.log("\n=== 失败明细 ===");
    console.log("  a.active=" + a.active + " !b=" + !b.active + " c=" + c.active +
                " !d=" + !d.active + " e=" + (e.started && e.active) +
                " !f=" + !f.active + " g.active=" + g.active +
                " g.track=" + g.calls.loadTrack.length +
                " g.post=" + g.calls.post.length +
                " fillOK=" + fillOK + " yieldOK=" + yieldOK +
                " !j=" + !j.active + " k=" + k.active);
  }
  console.log("\n结果: " + (ok ? "PASS" : "FAIL"));
  process.exit(ok ? 0 : 1);
})();
