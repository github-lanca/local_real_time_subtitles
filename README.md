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
8. **边看边落盘，看第二遍不再跑识别**：字幕同时写进视频同目录的 `.zh.srt`，从第二遍起 IINA 直接加载它（见下）。

### 字幕缓存：看到哪、补到哪

第一遍观看时每块的识别结果**立刻**累加写入视频同目录下的 `.live-ai/` 子目录，不需要看完、也不需要"完成"这个状态：

```
影片目录/
├── 影片.mp4
└── .live-ai/
    ├── 影片.srt       # 标准 SRT，边看边更新，随时能打开
    └── 影片.ranges    # 已处理过的时间区间（JSON）
```

下次打开同一部片：缓存里有的区间**秒回**（不跑 ffmpeg/ASR/翻译），没有的照常识别并补写回去。只看过 30:00–30:20 也没关系，那 20 分钟就是缓存的全部内容，剩下的边看边补。

**`.ranges` 记的是什么**：片内绝对时间（秒），和 SRT 里的时间轴一一对应 ——
`[[55.69, 57.42]]` 就是 SRT 里 `00:00:55,690 --> 00:00:57,420` 那一条。
它表示"这段已经处理过"，**不等于"这段一定有字幕"**（处理过但没人说话也是处理过）。

**为什么要 `.ranges`**：光有 SRT 只能证明"有对白的地方处理过"，无法区分"这段是静音"和"这段还没看"。记下区间之后，第二遍才能对空白窗口也直接作答，而不必每次重跑 ffmpeg 去确认没人说话。

**为什么必须放在 `.live-ai/` 子目录**（2026-11-22 修正）：IINA/mpv 的自动加载是**文件名模糊匹配** —— 只要字幕名里含视频名就会自动加载并选中。旧版平铺在视频目录的 `影片.live-ai.srt` 正好命中，被 IINA 当成外挂字幕加载，插件就误判"用户已有字幕"而跳过 AI；可缓存只覆盖看过的部分，播到没覆盖的地方屏幕全空。放进子目录后 mpv 不递归扫描，IINA 再也抢不到。旧文件会自动迁移（移动，不丢数据）。

| 场景 | 行为 |
|---|---|
| 只看中段就关掉 | 那段立即成为缓存；下次打开该段秒回，其余实时补 |
| 拖进度条跳过 | 跳过的区间没有记录，下次仍会识别（不会假装有） |
| 跳着看完一遍 | 缓存只有看过的那几段，第二遍继续补未看部分 |
| 想给别的播放器用 | 插件菜单「导出为标准字幕文件」→ 生成 `<影片>.zh.srt` |
| 想重来一遍 | 删掉 `.live-ai/` 目录（或其中对应的 `<影片>.srt/.ranges`） |
| 某段没字幕，想查原文 | 看 `logs/untranslated.log`（识别成功但翻译失败的会记在这里） |

**实测**：从 30:00 看 32 秒后关掉重开，已看区间全部命中（`cache hit ... 0.000s`），未看区间正常识别；旧版 `.zh.srt.part` 首次启动会自动迁移成新格式（用户现存 421 条实测迁移成功）。

**时间轴一定连续、不重叠（2026-11-22 修正）**：字幕是每 4 秒一块独立识别再累加的，窗口末尾那条字幕原先允许往后再补 0.5 秒，结果正好压到下一窗口开头那条上 —— 实测 SNOS-093 的缓存 381 条里有 **76 条时间重叠**，读起来就是"时间序列不连续"。现在：
- 每条 cue 严格限制在它自己的 4 秒窗口内，相邻窗口天然不重叠；
- 每次写盘前再全局整理一遍（`_normalize`）：后一条不早于前一条结束，前一条够长就截尾巴，太短就把后一条后推；
- 历史缓存会在 bridge 重启后第一次载入时被自动整理重写（实测 398 条 → 重叠 0 条）。

**字幕只保留三种符号：`...` `?` `~`（2026-11-22 用户要求）**：中文/假名/数字正常显示，其它标点（，。！、；：引号括号破折号斜杠等）**一律剔除**，不替换成别的符号。
- `…`/`……` → `...`；`？` → `?`；`～`/`〜` → `~`；
- 实现分四层：bridge 生成时、写缓存前、每次写盘前、插件显示前（`normalize_punct` + `cleanPunct`），任何一层漏掉都会被后一层兜住；
- 历史缓存同样在 bridge 重启后自动清洗重写（实测 SNOS-093 全片 536 条，违规字符 0）；
- 副作用（预期内）：数字间的小数点会被删（`3.5` → `35`），因为"只允许三种符号"。

### 打开影片时的字幕判定规则（2026-11-22）

顺序固定，命中哪条就停在哪条：

1. **IINA 已经加载并选中了字幕轨**（内嵌轨，或用户手动加载的外挂字幕）→ 完全不用 AI 字幕；
2. 网络串流 → 不用（本地文件才能抽音轨）；
3. **视频同目录存在用户自带的外挂字幕文件**（`<影片名>.srt` / `<影片名>.chs.srt` / `<影片名>.zh-CN.ass` 这类，扩展名 srt/ass/ssa/vtt/sub）→ 不用 AI 字幕，屏幕提示文件名。注意：**我们自己生成的 AI 产物不算用户字幕**（`.live-ai/` 子目录里的缓存、导出的 `<影片名>.zh.srt`、旧版 `.live-ai.srt`/`.part`），这些出现时插件照常接管；
4. 画面里有烧进去的硬字幕（截图 OCR 判定）→ 不用 AI 字幕；
5. 以上都没有 → **走 AI**：先读 `.live-ai/` 里的 AI 字幕缓存（有的区间秒回），缺的边看边识别补写；暂停时自动把后面补齐。

一句话：**用户自带字幕就不插手；AI 生成的字幕文件当作可续写的底稿**。

### 暂停时后台补全（2026-11-22）

播放时预取有个硬上限：只允许领先播放头 `LEAD(12s)`，所以一暂停管线就停了 ——
"看了 10 分钟就去睡觉"的结果是字幕文件也只有 10 分钟。

现在改成：**一暂停就继续往后啃**，一个窗口接一个窗口推到片尾，全程 `save=true` 写进 `.live-ai/` 缓存。

| 状态 | 预取上限 | 窗口 |
|---|---|---|
| 播放中 | `播放头 + LEAD(12s)` | 4s |
| 暂停中 | 片尾 | 4s（`PAUSE_CHUNK`，可调） |

- **恢复播放 / 拖动进度条**：立刻停止往后啃，通道让回实时请求（最多等当前那一个窗口 ≈2s）。已补的内容都在 `cues` 里，恢复后直接按时间显示，不会重复请求。
- **开关**：菜单「AI 字幕：暂停时后台补全（开 / 关）」，默认开。
- **进度**：菜单「AI 字幕：补全进度」→「已补到 12:34 / 1:58:00（11%）」。
- **速度**：单窗口 1.5–2.5s（双路同时播时会排队），约 2× 实时 —— 1 小时片暂停约 30 分钟补完。
- **为什么不加大窗口**：实测同一片四个位置，6s 窗口耗时 2.9–3.5s，4s 窗口 2.9–3.4s（瓶颈是固定开销不是音频长度），但 6s 更容易把重复拟声喂成退化（`うんうんうんうん`，`degenerate=true`）。所以靠"不停下"提速，不靠"窗口更大"。
- **双窗口注意**：bridge 的推理是全局串行（见 AGENTS.md 4.2b）。一个窗口暂停补全、另一个窗口仍在播放时，播放中的那一路要多等约一个窗口（~2s）。这种情况用菜单临时关掉补全即可。

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
│   ├── hardsub_check.py      # 画面硬字幕检测（底部字幕带 + Vision OCR）
│   ├── detach.py             # setsid 启动，避免服务随启动方退出
│   ├── probe.py              # 单窗口诊断：VAD 多阈值 + 原始识别 + 当前判定
│   ├── report_logs.py        # 汇总：出字幕/丢弃统计、可疑丢弃
│   ├── bench_bridge.py       # 按插件节奏压窗口，测延迟
│   ├── audit_asr.py          # 与 faster-whisper CT2 对准确率
│   ├── test_sidecar.py       # 字幕文件自检：写入/去重/完成判定/续读
│   └── sim_autostart.js      # 假 IINA 环境回归"自动接管"与断点保存逻辑（9 场景）
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
| `python3 tools/audit_untranslated.py [--apply]` | 重译 `untranslated.log` 里的条目：能译的归档、译不出的留下并给原因 |
| `python3 tools/rebuild_ranges.py <video> [--dry]` | 按字幕条目重建 `.ranges`（区间与字幕对不上时用） |

判读要点：`drops.log` 里 `vmax` 很高却被丢弃 = 可能误丢；`vmax<0.18` = 确实没人声。

`logs/untranslated.log` 是**校正翻译逻辑的输入**：每改一次提示词/过滤规则，跑一次
`audit_untranslated.py --apply`，能译的会归档到 `untranslated.resolved.log`（带译文与解决时间），
文件清空才算这轮问题真的修完。

---

## 9. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 服务起来了但字幕时快时慢 | 先看 `sysctl -n vm.swapusage`。swap 接近上限时 MLX 权重被换出，会从 600fps 掉到 45fps |
| `Address already in use` | 上一个 bridge 还没完全退出：`lsof -nP -iTCP:18182 -sTCP:LISTEN -t \| xargs kill`，再启动 |
| 插件改了代码不生效 | IINA 只在启动时加载插件：设置 → 插件 → 选中本插件 → **Reload**，或重启 IINA |
| 打开片子没反应 | 看 `logs/plugin.log`：`skip: subtitle showing` 表示检测到已有字幕轨；`auto: ... -> activate` 才是接管 |
| 某些段落只有「啊～」 | 该段是连续气声/非语言发声，本地模型听不出稳定文字（mlx 各温度都循环、CT2 需 17–22s 且不稳定）。这是有意的降级 |
| 第二遍看同一个片子，某些段落还在跑识别 | 正常——那些区间上一遍没看过。查 `curl "http://127.0.0.1:18182/subtitle?video=<urlencode>&duration=<秒>"`，`gaps` 就是还没缓存的区间 |
| 片源自带"烧进去"的字幕，AI 字幕还叠上来 | 开播时会自动检测：只裁底部 18% 字幕带做 macOS Vision OCR，命中就跳过（日志 `skip: burned-in subtitle detected`）。想手动切换用插件菜单「AI 字幕：开 / 关」 |
| 字幕出现"这句话包含了一些不清晰的发音…我无法直接翻译"这类说明 | 说明模型在讲道理而不是翻译。已用两层拦住：提示词明确"不要拒绝/不要要求澄清"，输出端再按话术特征丢弃；被丢掉的原文会进 `logs/untranslated.log` 供你校对 |
| 字幕里出现英文（byebye / OK） | 有意保留：日语原文就是外来语，硬译成中文反而失真。≤24 字符的纯字母译文直接放行 |
| 字幕里夹了少量日文（人名、昵称） | 有意保留：专名硬译反而更难懂（`カンナちゃん` → 「カンナ酱」）。常见片假名词会先替换（`ダメくん`→废柴君）；整句回吐日文的仍会被拒绝 |
| 字幕偶尔出现日文 | 已加译文校验（含假名即判失败并重试）；若仍出现，看 `logs/films/` 里对应的 `[识别]` 行反馈 |

---

## 10. 已知限制

- **单语种假设**：强制 `language="ja"`。多语种片子会被识别成假名乱码（已用"无日文字符即丢弃"兜住）。
- **不区分说话人**：靠 `initial_prompt` 上下文保持语气连续。
- **极端段落降级**：纯气声/连续非语言发声 → 「啊～」或跳过（本地无模型能在实时预算内转出文字）。
- **16GB 是上限**：同时开浏览器等重负载应用会挤占内存，表现为延迟抖动。
- **缓存有洞是正常的**：只看过哪几段就只缓存哪几段，没看过的仍会实时识别。
- **翻译是 3B 小模型**：长句偶有省略或语气生硬，可通过 `SYS_PROMPT` 的 few-shot 例句继续调。
- **窗口偶发拖不动**：IINA 的插件 overlay 是一整块覆盖视频区的 WKWebView，官方 API 虽标称点击穿透，但 IINA 有已知同类问题（插件 overlay 打断鼠标/拖放事件链）。双显示器又默认 `spans-displays=0`（独立空间），全屏窗口本身也拖不到另一屏。
  排查顺序：① 先退出全屏（`f` / `Esc`）；② 菜单「AI 字幕：临时隐藏字幕层（排查窗口拖动）」→ 能拖就确定是 overlay，隐藏后我再改成"只在有字幕时 `show()`、空字幕立刻 `hide()`"；③ 仍不行则在 IINA 设置里临时禁用本插件对比。

---

## 11. 开发笔记

完整的踩坑记录、判据推导与实测数据在 **[AGENTS.md](AGENTS.md)**（含 30 余条"别再重复"的结论）。
