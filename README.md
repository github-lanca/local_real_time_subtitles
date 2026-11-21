# local_real_time_subtitles

**用 IINA 看日语片时的本地实时中文字幕。** 不联网、不调 API、零费用：音频在 Mac 上识别、在本机翻译，字幕以浮层直接叠在画面上。

打开一个没有字幕的片子，几秒内自动开始出字幕；有字幕的片子自动跳过；播放中途拖动、倍速、换片都能跟上。

---

## 1. 实测效果（Apple M4 / 16GB）

| 指标 | 实测值 |
|---|---|
| 单块识别耗时 | **0.9–1.3 s** / 4 s 音频（mlx-whisper fp16，约 600 帧/秒） |
| 单个请求总耗时 | **1.2–2.3 s**（ffmpeg 0.3s + VAD 0.02s + 识别 1.0s + 翻译 0.1–0.5s） |
| 预取领先播放头 | 12–18 s，正常播放无 `resync` |
| 音乐/纯静音块 | 0.3 s 返回，不出字幕（不再空跑十几秒编造台词） |
| 日语准确率 | 与 faster-whisper CT2(beam=5) 交叉验证 9 个窗口：4 窗逐字一致，2 窗本方案更好（CT2 反而幻觉出「ご視聴ありがとうございました」） |
| 内存常驻 | mlx 权重 1.6G + qwen 3B 约 2.0G ≈ 3.6G |

---

## 2. 复现基准环境

另建同款设备时，按这张表对齐即可。

| 项目 | 版本 / 规格 |
|---|---|
| 机型 | Apple M4，16 GB 统一内存（**内存是这个方案最紧的资源**） |
| 系统 | macOS 27.3 |
| 播放器 | IINA **1.5.1**（插件 API 依赖此版本，`core.subtitle.currentTrack` 等字段） |
| Python | Homebrew Python **3.14.6**（`/opt/homebrew/bin/python3`） |
| ffmpeg | Homebrew **8.1.2**（`/opt/homebrew/bin/ffmpeg`） |
| Node | 22.x（只用于 `node --check` 语法校验） |
| 语音识别 | `mlx-whisper==0.4.3` + `mlx-community/whisper-large-v3-turbo`（fp16，1.6G） |
| 语音门禁 | Silero VAD v6（`models/vad/silero_vad_v6.onnx`，1.1M，随仓库提供） |
| 翻译模型 | `qwen2.5-3b-instruct-q4_k_m.gguf`（2.0G）+ llama.cpp **b12602** 预编译 |
| 端口 | 18180 whisper-server（可选回退）/ 18181 llama-server / 18182 bridge |

> 依赖包版本见 `requirements.txt`，模型文件不进仓库，用 `tools/fetch_models.sh` 一键下载（全部走国内镜像）。

---

## 3. 架构

```
IINA 播放头 position
   │  每 250ms 轮询（插件）
   ▼
live-ai-subs/main.js ──POST /transcribe {video,start,duration,context}──► bridge :18182
   │                                                                        │
   │◄──────────── {ok, cues:[{start,end,zh}], ja, vocal} ───────────────────┘
   │                             │
   │                             ├─① ffmpeg 抽 6.5s 音频 + loudnorm 动态归一化
   │                             ├─② Silero VAD：有没有人声 + 真实语音段
   │                             ├─③ mlx-whisper fp16：贪心解码 → 退化检测 → 有界重试
   │                             ├─④ qwen 逐句翻译（本地 llama.cpp），译文必须是中文
   │                             └─⑤ 幻觉过滤 + 边界对齐 + 最短停留 + 缓存
   ▼
overlay.setContent() 按 cue 时间窗渲染浮动字幕
```

| 组件 | 职责 |
|---|---|
| `live-ai-subs/` | IINA 插件。判断"有没有字幕在显示"，没有就自动拉起服务并接管；负责预取、渲染、拖动/倍速、去重 |
| `bridge_server.py` | 单点编排（:18182）。切音频 → VAD → 识别 → 翻译 → 生成 cue；全局锁串行化，分阶段计时 |
| `vad_silero.py` | 独立 Silero VAD v6（onnxruntime）。只回答"这段有没有人声""人声在哪几段" |
| `llama-server` | 翻译（:18181），qwen2.5-3b，`-c 2048` 省内存 |
| `start_services.sh` / `stop_services.sh` | 启停。用 `tools/detach.py`（setsid）启动，启动方退出后服务仍存活 |

---

## 4. 关键设计（为什么这么做）

1. **吞吐铁律：单请求耗时必须 < CHUNK(4s)。** 否则预取追不上播放头，字幕回来时时间窗已过，永远不渲染（表现为日志刷 `resync`、屏幕上什么都没有）。
2. **解码三段式**：`temperature=(0.0,)` 贪心先跑（1s，普通对白窗与全温度阶梯**逐字一致**）；检测到重复退化（`compression_ratio>4`／同字符≥6 连／单元重复／词汇坍缩）才用 `(0.2,0.4)` 有界重试。默认温度阶梯在"短窗+静音尾"会退化，一次烧 9–15s。
3. **VAD 三元门禁**（关键）：`maxprob<0.18` 判静音直接跳过；`0.18–0.5` 视为弱语音，仍跑识别但**只接受拟声结果**（啊～）；`≥0.5` 正常识别。这样音乐/环境音 0.3s 返回，而气声台词不会被丢掉。
4. **边界由 VAD 定，不信 Whisper 时间戳**：Whisper 的 segment 时间是"均匀摊平"的假值。句子数 > 语音段数时按语音时长把碎段**分组**；每条字幕末尾再补 0.5s（`CUE_TAIL_PAD`），解决"字幕比台词早消失"。
5. **译文必须是中文**：qwen 对短句会原样回日文（实测 `割れちゃうから` → `裂けちゃうから`）→ 校验含假名即判失败，严格重试一次，仍失败则不出字幕。
6. **16GB 内存是硬约束**：mlx 1.6G + qwen 2.0G 已是上限。**swap 打满会让 MLX 从 600fps 掉到 45fps**（同一段音频 14s vs 1.0s），所以"变慢了"第一件事是查 `sysctl -n vm.swapusage`。
7. **全自动 + 自愈**：IINA 一开播就判定（等 duration + 静置 600ms，用 `currentTrack`/`id` 判断"是否真的在显示"）；服务没起自动执行启动脚本；连续 4 次请求失败自动重启服务。

---

## 5. 目录结构

```
.
├── bridge_server.py          # 编排服务 :18182（识别+翻译+出字幕）
├── vad_silero.py             # Silero VAD 封装（门禁 + 边界）
├── start_services.sh         # 启动 llama + bridge（setsid detach）
├── stop_services.sh          # 按端口停服
├── requirements.txt
├── AGENTS.md                 # 开发笔记：完整版踩坑记录与调参手册
├── live-ai-subs/             # IINA 插件（Info.json + main.js）
│
├── tools/
│   ├── fetch_models.sh       # 一键下模型（国内镜像）
│   ├── detach.py             # setsid 启动，避免服务随启动方退出
│   ├── probe.py              # 单窗口诊断：VAD 多阈值 + 原始识别 + 当前判定
│   ├── report_logs.py        # 汇总：出字幕/丢弃统计、可疑丢弃
│   ├── bench_bridge.py       # 按插件节奏压窗口，测延迟
│   ├── audit_asr.py          # 与 faster-whisper CT2 对准确率
│   └── sim_autostart.js      # 假 IINA 环境回归"自动接管"逻辑（5 场景）
│
├── models/                   # 不进仓库（除 vad/）
│   └── vad/silero_vad_v6.onnx
└── logs/                     # 不进仓库
    ├── bridge.log            # 每块耗时分解 + 丢弃原因 + 原始日语
    ├── drops.log             # 只记被丢弃的块
    └── films/<影片名>.log     # 可读主日志：识别 / 字幕 / 放弃
```

---

## 6. 从零部署（另一台同款设备）

```bash
# 0) 依赖
brew install ffmpeg node
/opt/homebrew/bin/python3 -m pip install -r requirements.txt \
  --user --break-system-packages -i https://mirrors.aliyun.com/pypi/simple/

# 1) 克隆到与源机器相同的路径（插件里写死了这个路径，见下方"换路径"）
mkdir -p $HOME/AI && git clone https://github.com/github-lanca/local_real_time_subtitles.git $HOME/AI/实时字幕
cd $HOME/AI/实时字幕

# 2) 下载模型（mlx 权重 / qwen gguf / llama.cpp 二进制，全部走镜像）
bash tools/fetch_models.sh

# 3) 启动服务并等就绪（首次约 20s：加载权重 + 编译 Metal kernel）
bash start_services.sh
curl -s http://127.0.0.1:18182/health
# ↑ 期望 {"ok":true,"backend":"mlx","whisper":true,"mlx_ready":true,"llama":true}

# 4) 装插件（符号链接即可，开发模式）
iina-plugin link "$PWD/live-ai-subs"
defaults write com.colliderli.iina "PluginEnabled.com.lanca.live-ai-subs" -int 1

# 5) 重启 IINA，打开一个没有字幕的片子
```

**换路径**（没克隆到 `$HOME/AI/实时字幕` 时）：
- 插件里改一行：`live-ai-subs/main.js` 顶部的 `const SUBS_ROOT = "...";`
- 脚本可用环境变量：`export SUBS_ROOT=/your/path`

### 验收清单

```bash
# ① 服务健康
curl -s http://127.0.0.1:18182/health

# ② 单次识别+翻译（用片子里任意有人声的位置，避免命中缓存）
curl -s http://127.0.0.1:18182/transcribe -X POST -H 'Content-Type: application/json' \
  -d '{"video":"/path/to/film.mp4","start":100.0,"duration":4.0,"context":"","language":"ja"}'
# ↑ 期望 ok=true，cues 里是中文

# ③ 插件是否接管（重启 IINA 后看日志）
tail -f logs/plugin.log      # 应出现 "auto: no subtitle showing ... -> activate" 与 CUE/RENDER

# ④ 性能基准（12 个窗口）
python3 tools/bench_bridge.py /path/to/film.mp4 100 12 4.0
# ↑ asr 列应 ~1.0s，wall 列应 < 2.5s
```

---

## 7. 配置项（全部通过环境变量，改完重启 `start_services.sh`）

| 变量 | 默认 | 作用 |
|---|---|---|
| `ASR_BACKEND` | `mlx` | `mlx`（默认）/ `http`（whisper.cpp 回退）/ `faster`（离线对比） |
| `MLX_MODEL` | 自动发现本地 HF 快照 | 指定 mlx 权重目录（**传目录，别传 repo id**，否则每次识别都走网络） |
| `SUBS_ROOT` | 脚本所在目录 | 仓库根目录 |
| `VAD_THRESHOLD` | `0.5` | 判定"有人声"的阈值 |
| `VAD_WEAK_MIN_PROB` | `0.18` | 低于此值算静音/环境音；高于则走弱语音（只收拟声） |
| `VAD_WEAK_PROB` | `0.15` | 弱语音模式下定位发声段用的低阈值 |
| `MOAN_MIN_SEC` | `0.8` | 弱语音块至少要这么长才输出「啊～」 |
| `MIN_CUE_DISPLAY` | `1.0` | 单条字幕最短停留（不会被下一句压掉） |
| `CUE_TAIL_PAD` | `0.5` | 字幕末尾补偿（VAD 常提前收尾） |
| `MLX_CACHE_MB` | `512` | MLX Metal 缓冲上限，内存紧张就调小 |
| `BRIDGE_TRACE` | `1` | 输出 `[brk]` 分阶段日志 |

拟声词对照表在 `bridge_server.py` 的 `VOCAL_TABLE`（`あ→啊～`、`ん→嗯～`、`は→哈～`、`あら→哎呀～`），按喜好直接改。

---

## 8. 日志与调参

**一部片子一个文件**，每行 = 当前时间 + `T+时:分:秒`（影片时间）+ 三段之一：

```
2026-11-21 21:12:49  T+00:13:24.00  [识别] サイズがなかなかぴったりなサイズがないんですよね
2026-11-21 21:12:49  T+00:13:24.27  [字幕] 找不到合适的尺寸   [00:13:24.27 → 00:13:27.31]
2026-11-21 21:13:11  T+01:58:20.00  [放弃] silence — VAD 判定没有语音（静音/纯环境音）   (vmax=0.02 sp=0.00)
```

| 命令 | 用途 |
|---|---|
| `python3 tools/report_logs.py -f` | 看最新影片日志 |
| `python3 tools/report_logs.py 60` | 最近 60 块：出字幕/丢弃统计 + 可疑丢弃 |
| `python3 tools/probe.py <video> <秒>` | 单个时间点：VAD 多阈值分布、原始识别、**当前规则会怎么判**、改阈值会怎样 |

判读要点：`drops.log` 里 `vmax` 很高却被丢弃 = 可能误丢；`vmax<0.18` = 确实没人声。

---

## 9. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 服务起来了但字幕时快时慢 | 先看 `sysctl -n vm.swapusage`。swap 接近上限时 MLX 权重被换出，会从 600fps 掉到 45fps |
| `Address already in use` | 上一个 bridge 还没完全退出：`lsof -nP -iTCP:18182 -sTCP:LISTEN -t \| xargs kill`，再启动 |
| 插件改了代码不生效 | IINA 只在启动时加载插件：设置 → 插件 → 选中本插件 → **Reload**，或重启 IINA |
| 打开片子没反应 | 看 `logs/plugin.log`：`skip: subtitle showing` 表示检测到已有字幕轨；`auto: ... -> activate` 才是接管 |
| 某些段落只有「啊～」 | 该段是连续气声/非语言发声，本地模型听不出稳定文字（mlx 各温度都循环、CT2 需 17–22s 且不稳定）。这是有意的降级 |
| 字幕偶尔出现日文 | 已加译文校验（含假名即判失败并重试）；若仍出现，看 `logs/films/` 里对应的 `[识别]` 行反馈 |

---

## 10. 已知限制

- **单语种假设**：强制 `language="ja"`。多语种片子会被识别成假名乱码（已用"无日文字符即丢弃"兜住）。
- **不区分说话人**：靠 `initial_prompt` 上下文保持语气连续。
- **极端段落降级**：纯气声/连续非语言发声 → 「啊～」或跳过（本地无模型能在实时预算内转出文字）。
- **16GB 是上限**：同时开浏览器等重负载应用会挤占内存，表现为延迟抖动。
- **翻译是 3B 小模型**：长句偶有省略或语气生硬，可通过 `SYS_PROMPT` 的 few-shot 例句继续调。

---

## 11. 开发笔记

完整的踩坑记录、判据推导与实测数据在 **[AGENTS.md](AGENTS.md)**（含 30 余条"别再重复"的结论）。
