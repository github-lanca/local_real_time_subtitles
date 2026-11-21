// Smoke-test the plugin's auto-activation state machine outside IINA.
// Loads the real main.js with a fake `iina` global, drives ticks manually and
// asserts what the plugin decided.
const fs = require("fs");
const vm = require("vm");
const path = require("path");

const SRC = path.join(__dirname, "..", "live-ai-subs", "main.js");

function makeEnv(scenario) {
  const calls = { osd: [], setContent: [], exec: [], post: [], logs: [], menu: [], healthLeft: scenario.healthDown || 0 };
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
        setContent: (h) => calls.setContent.push(h),
      },
      http: {
        get: async (url) => {
          if (String(url).indexOf("/hardsub") >= 0) {
            calls.hardsubQueries = (calls.hardsubQueries || 0) + 1;
            return { text: JSON.stringify({ ok: true, hardsub: !!scenario.hardsub, samples: [] }) };
          }
          if (scenario.healthDown && calls.healthLeft-- > 0) throw new Error("ECONNREFUSED");
          return { text: JSON.stringify({ ok: true, whisper: true, llama: true }) };
        },
        post: async (url, opts) => {
          calls.post.push(opts.data);
          return { text: JSON.stringify({ ok: true, ja: "テスト", zh: "测试", cues: [{ start: opts.data.start + 0.5, end: opts.data.start + 1.5, zh: "测试" }] }) };
        },
      },
      utils: { exec: (file, args) => { calls.exec.push([file].concat(args || [])); return { status: 0 }; } },
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

  const ok = a.active && !b.active && c.active && !d.active && e.started && e.active && !f.active;
  console.log("\n结果: " + (ok ? "PASS" : "FAIL"));
  process.exit(ok ? 0 : 1);
})();
