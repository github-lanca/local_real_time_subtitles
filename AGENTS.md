# AGENTS.md — 实时字幕项目

> 本项目为 IINA 播放器外挂一个本地 AI 实时字幕插件：播放无字幕的日语影片时，
> 提前几秒抽取音频 → 本地语音识别 → 本地翻译成中文 → 用 IINA overlay 浮动渲染。
> 全程离线、只读取本地影片音轨，不上传、不传播。

本文件供后续维护者（人或 AI agent）快速接手：架构、模型选型、踩过的坑、如何启动与调试。

---

## 1. 目录结构

```
$HOME/AI/实时字幕/
├── AGENTS.md                  # 本文件
├── start_services.sh          # 幂等启动：翻译服务 + bridge
├── stop_services.sh           # 按端口停止所有服务
├── bridge_server.py           # 核心桥接服务（本地 HTTP :18182）
├── live-ai-subs/              # IINA 插件源码
│   ├── Info.json              # 插件元数据 + 权限声明
│   └── main.js                # 插件主体：轮询播放头、预取、渲染
├── live-ai-subs-1.0.0.iinaplgz # 打包产物（开发时用 link，不依赖它）
├── models/                    # 所有模型文件（见第 3 节）
├── tools/llama/llama-b12602/  # llama.cpp 官方预编译二进制
├── logs/                      # whisper / llama / bridge 日志
└── .workbuddy/memory/         # 项目记忆与每日日志
```

---

## 2. 架构

### 2.1 数据流

```
IINA 播放头 position
   │  (每 250ms 轮询)
   ▼
插件 main.js ──POST /transcribe {video,start,duration,context}──► bridge :18182
   │                                                                  │
   │  ◄──────── {ok, cues:[{start,end,zh}], ja} ────────────────────────┘
   │                             │
   │                             ├─① ffmpeg 抽音频 + loudnorm 动态增益
   │                             ├─② Silero VAD 门禁 + 语音段（cue 边界来源）
   │                             ├─③ mlx-whisper fp16 日语识别（贪心+退化重试）
   │                             ├─④ qwen 逐句翻译成中文（HTTP :18181）
   │                             └─⑤ 幻觉过滤 + cue 裁剪 + 缓存
   ▼
overlay.setContent() 按 cue 时间窗渲染浮动字幕
```

### 2.2 roles

- **插件 `main.js`（浏览器 JS 环境）**：不直接推理。负责检测「当前文件有没有字幕轨」；无字幕则拉起服务、轮询 `core.status.position`、按 `CHUNK=4.0s` 前瞻预取、按 cue 时间渲染、处理拖动/倍速/文本去重。所有推理通过本地 HTTP 交给 bridge（绕开 IINA http 模块不支持 multipart 的限制）。
- **`bridge_server.py`（Python，进程内 ASR）**：单点编排。ffmpeg 切音频 → Silero VAD 门禁 → mlx-whisper 识别 → qwen 翻译 → 返回带真实边界的 `cues`。全局锁串行化请求（日志里 `lock=` 即排队时间），`_cache` 按 `video|start|duration|language|context` 缓存。
- **`vad_silero.py`**：独立 Silero VAD v6（onnxruntime）封装，只做「有没有人声 + 语音段边界」。
- **`llama-server`（:18181）**：只负责翻译（qwen2.5-3b）。
- **whisper-server（:18180）**：回退 ASR 后端（`ASR_BACKEND=http`），mlx 出问题时用它。

### 2.3 端口

| 端口 | 服务 | 状态 |
|---|---|---|
| 18180 | whisper-server（ASR 回退，`ASR_BACKEND=http` 时才启动）| 可选 |
| 18181 | llama-server（翻译）| 常驻 |
| 18182 | bridge_server.py | 常驻 |

### 2.4 关键实现约定

- **预取节奏**：`CHUNK=4.0s`，`LEAD=8s`（按倍速放大），首条从播放头位置开始，约 3 秒后出结果。**铁律：单请求处理耗时必须 < CHUNK**，否则预取追不上播放头、字幕永不渲染（历史上 CHUNK=2.5 配 3s 处理就栽在这里）。
- **时间对齐**：bridge 抽 `[start, start+4.0+2.5]`，多出的 2.5 秒 `LOOKAHEAD_SEC` **只作识别上下文**（后一句能帮模型听清前一句），字幕 cue 严格裁剪到 `[start, start+4.0]` 内。边界由 Silero 语音段定，不信 Whisper 假时间戳。
- **resync**：仅当 `nextTarget < pos - LEAD*0.8`（约 6.4s）才重置，避免轻微落后就回退导致永久追不上。
- **拖动/倍速**：seek 用 `abs(delta - speed*dt) > max(0.7, speed*dt*0.6)` 判定（而非写死阈值）；拖动后 `token++` 丢弃所有在途旧结果；倍速时前瞻量随速度放大；>3x 提示可能延迟。
- **去重**：插件端 `normText()` 去标点后，6 秒窗口内相同文本只保留一条（防相邻切片重复显示「早上好/早上好。」）。
- **字幕让位**：每 2 秒复查字幕轨，一旦用户加载字幕即停用 AI 字幕。
- **自动接管（IINA 一开播就生效）**：轮询发现 `core.status.url` 变化即进入「待判定」状态，(1) 等 duration > 10s 且 `position > 0.15`（或已等 1.5s），(2) 再静置 `SETTLE_MS=600ms` 让 IINA 把内嵌轨道列完，(3) 用 `subtitleState()` 判定：`tracks.length > 0 && (id > 0 || currentTrack)` 才算"有字幕在显示"。没有 → 调 `activate()`（内部 `ensureServices()` 会检查 127.0.0.1:18182，不通就执行 `start_services.sh`）。网络串流直接跳过。判定**每个文件只做一次**（`decidedPath`），避免反复启停。
- **服务自愈**：连续 4 次请求传输失败 → 认为 bridge 已死，静默重跑 `ensureServices()`（20s 节流）。
- **幻觉防护**：四层——① Silero VAD 门禁（无真人声直接不推理，音乐窗 0.3s 返回）② 退化检测（`compression_ratio>4` / 同字符≥6 连 → 有界重试，人声不足则丢弃）③ 幻觉短语黑名单 ④ cue 裁剪到真实发声区间。**Whisper 的 `no_speech_prob` 不可信**（静音上也会给 0.000）。

---

## 3. 模型选型与优缺点

### 3.1 语音识别（ASR）

| 方案 | 结论 | 优点 | 缺点 / 为何弃用 |
|---|---|---|---|
| **mlx-whisper + `mlx-community/whisper-large-v3-turbo` fp16**（进程内，Metal）| ✅ **当前默认（`ASR_BACKEND=mlx`）** | 日语最准（难词「即死」「すごい」正确）；**1.0–1.1s / 6.5s 窗口**（650 帧 ≈ 600fps）；权重常驻、无 server；`initial_prompt` 可注入上下文 | 短窗口 + 尾部静音会**重复退化**（`あ、あ、あ…`，见坑 23）；fake 时间戳（用 Silero 定边界）；1.6GB 常驻内存 |
| faster-whisper large-v3-turbo CT2（`deepdml/...-ct2`，int8，CPU）| ✅ **离线基准/交叉验证用**（`ASR_BACKEND=faster`），**非实时** | 日语识别最准之一；真实词级时间戳；进程内调用无需 server | **致命**：CPU 上每次 `transcribe()` 有 ~5.7–6.8s 固定开销，CT2 无 Metal → 追不上播放，字幕永不渲染 |
| whisper.cpp `ggml-large-v3-turbo-q5_0`（whisper-server :18180）| ⚠️ 已验证的稳定回退（`ASR_BACKEND=http`）| Metal，~2.5–3s/请求；Q5 仅 547MB；经实战验证不退化 | 准确率低于 mlx（难词听错）；每请求 ~3s 贴近 CHUNK 上限；静音幻觉严重 |
| kotoba-whisper-v2.0-faster | ❌ 弃用 | 日语专精 | `word_timestamps=True` 崩溃 `ValueError: vector`（ctranslate2 版本不兼容）；增益处理后 VAD 返回空 |
| mlx 8bit / q4 量化 | ❌ 弃用 | 省内存 | 8bit 是 `model.safetensors`，mlx-whisper 0.4.3 加载失败；q4（npz）能加载但**准确率崩坏**（重复字/听错） |

**为什么最终是 mlx-whisper fp16**：实时字幕的硬约束是**每请求处理耗时 < CHUNK**，而精度第一的目标只有 fp16 大模型能满足。mlx 在 M4 上 1.0s/窗口，比 whisper.cpp 的 3s 有 3 倍余量（余量用来吸收偶发的有界阶梯重试），且日语难词准确率明显更好。

**假时间戳的解法**：mlx 的 segment start/end 同样是「均匀摊平」的假值，cue 边界**完全由 Silero VAD 的真实语音分段决定**——句子数 ≤ 语音段数时一一对应，语音连续时把整段均分给各句。

### 3.2 翻译

| 方案 | 结论 | 优点 | 缺点 |
|---|---|---|---|
| **qwen2.5-3b-instruct-q4_k_m（llama.cpp）** | ✅ 当前选用 | 中英日翻译质量足够；3B Q4 仅 1.9GB；响应快（0.14s/句）；Metal 加速 | 提示词需明确「只输出译文、保留中文标点」否则丢标点/加解释 |
| qwen 1.5B Q4（备选）| 可选 | 省约 1GB 内存 | 翻译质量略降 |

### 3.3 模型文件

```
models/
├── vad/silero_vad_v6.onnx                # ✅ 语音门禁 + cue 边界（1.1M）
├── ggml-large-v3-turbo-q5_0.bin          # ⚠️ whisper.cpp 回退后端（547M）
├── ggml-silero-v6.2.0.bin / v5.1.2.bin   # 弃用（whisper.cpp --vad）
├── faster-whisper-large-v3-turbo/        # ✅ 离线基准/对比（CT2，1.6G）
├── qwen2.5-3b-instruct-q4_k_m.gguf       # ✅ 当前翻译（1.9G）
└── kotoba-whisper-v2.0-faster/           # 弃用

~/.cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo/snapshots/<hash>/
                                          # ✅ 当前 ASR 权重（fp16，1.61G）
```

### 3.4 资源占用（M4 / 16GB 实测）

- 常驻：mlx fp16 权重 1.61GB + llama-server ~1.9GB（`-c 2048`）≈ 3.5GB。
- **16GB 机器上这是硬约束**：两者同时常驻 + IINA 播 60fps 片 → swap 被打到 6GB/7GB，MLX 权重被反复换出、重读磁盘，于是「同一段音频忽快忽慢 1s/7s/14s」。**任何"偶发变慢"先查 `sysctl -n vm.swapusage` 和 `frames/s`，别猜 Metal shader**（见坑 23）。
- 单请求（mlx，实测）：ffmpeg 0.29–0.36s + Silero 0.01s + ASR 1.0–1.1s + 翻译 0.1–0.8s ≈ **1.5–2.3s**，< CHUNK 4s。
- 冷启动：模型加载 + Metal kernel 编译 ~10–20s，必须常驻（bridge 启动即后台预热）。

---

## 4. 踩过的坑（重要，别重复）

按主题归类，每条都是实际调试出来的结论。

### 4.1 幻觉类

1. **Whisper 在静音上幻觉出「ご視聴ありがとうございました」（谢谢观看）**，且 `no_speech_prob=0.000` 极「自信」。→ 结论：**不能信置信度，必须用 ffmpeg 音频能量做硬门禁**（`silencedetect=-42dB:d=0.25`），静音段直接不推理。
2. **旧 bridge 有整块 fallback**：幻觉句被挂满整个 6 秒窗口。→ 删除 fallback，宁可无字幕。
3. **overlap ≥0.4s 会在片尾拖尾静音上重新触发幻觉**，0.3s 正常（后来改 lookahead 4s 后有门禁兜底）。

### 4.2 时间戳类

4. **whisper.cpp 的 segment/词级时间戳是「均匀摊平」的假时间戳**：t=18 的「おはようございます」被摊到 5.99s，每句卡整秒边界 → 字幕挂太久、错位 3s。→ **最终解法不是换模型，而是不信它的时间戳**：cue 边界改用 ffmpeg `silencedetect` 的真实语音分段（voice 段一一对应各句，或连续语音时均分给各句）。
5. **切片太短会误听**：「これ即死じゃん」→「これ食事じゃん」。识别依赖**后续上下文**（后一句帮模型听清前一句），不是前文。→ 用 `LOOKAHEAD_SEC=1.5` 提供适度上下文；但注意它增大 ASR 输入、影响延迟（见坑 11）。

### 4.2b 实时性 / 吞吐类（最隐蔽的坑）

11. **「识别对了却没字幕」的真凶是预取吞吐**：插件日志满屏 `resync`，CUE 产出总比对应播放时间晚 → 结果回来时时间窗已过，从不 RENDER。**铁律：单请求处理耗时必须 < CHUNK**。CHUNK=2.5 配 3s 处理 → 永远追不上 → 无字幕。→ CHUNK 提到 4.0。
12. **faster-whisper CT2 的隐藏固定开销**：CPU 上每次 `transcribe()` 恒 ~6.5s，连 0.5s 静音都一样（与音频长度无关，非推理计算），CT2 无 Metal → 实时不可用，尽管它识别最准、有真词级时间戳。→ 默认回退 whisper.cpp（~3s/请求）。**选模型时「准确率」要让位于「延迟是否 < CHUNK」**。
13. **resync 阈值过激**：原 `nextTarget < pos-0.5` 每次轻微落后就重置到播放头，导致 cue 永远过期。→ 放宽到 `pos - LEAD*0.8`。
14. **JS 暂时性死区（TDZ）**：resync 逻辑引用了在函数后面才 `const` 定义的 `lead`，`ReferenceError: Cannot access 'lead' before initialization`，整个 tick 挂掉、字幕全停。→ 把 `const lead = leadFor(speed)` 移到使用前。改 JS 后务必 `node --check` 并看插件日志有无 `tick exception`。

24d-2. **气声/拟声段不要丢，要"只显示一次"**：`あ、あ、あ…`、`あらあらあら…`、`あーーーっあーーーっ…` 这类没有词、只有长音的段落，循环显示不可读、直接丢弃又让整场戏静音。→ `_vocal_zh()`：文本是**纯假名且带延长特征**（含 `ー`、单元重复、或 1–2 字元音重复≥3 次）时，直接产出中文一行：`あ→啊～`、`ん→嗯～`、`は→哈～`、`あら→哎呀～`，并且**在重试之前**就返回（重试尖叫只会多花 2.5–7.5s 换来同一声尖叫）。响应带 `"vocal": true`，插件据此不把它写进 context。实测同一窗口 12.74s → 1.48s。
24d-3. **气声段被 VAD 门禁挡在 ASR 之前**：`speech_sec < 0.35` 直接返回 silence，于是整段只有气声的戏**一条字幕都没有**（实测 maxprob 只有 0.20–0.30，但 Whisper 强制识别就是 `あ、あ、あ…`）。→ 加"弱语音旁路"：`vad_max_prob >= VAD_WEAK_MIN_PROB(0.18)` 时仍跑 ASR、把 VAD 阈值降到 0.15 定位，但**只接受拟声结果**（`vocal`），其它一律丢弃；<0.18 的真正环境音/静音仍直接跳过，避免噪音字幕。注意 loudnorm 之后对白与气声的 RMS 几乎一样（-22 dBFS），**不能靠能量区分**。
24d-4. **日语片里 Whisper 会冒英文"字幕惯用语"**：强制 `language="ja"` 时，纯气声/静音块仍可能输出 `I'm not going to die`、`Oh, my God`（训练语料里的字幕套话），而且会被翻译成中文显示出来。→ 判据极简且可靠：**强制 ja 模式下不含任何日文字符（假名/汉字）的输出，一律当幻觉丢弃**（`_has_japanese`），并在黑名单补上这几句英文。
24d-5. **气声/非语言发声的兜底与"别吞掉台词"是一对矛盾**：弱语音块若 ASR 没给出可用台词，就用 `MOAN_MIN_SEC(0.8s)` 判定"这段是个长音"→ 出 `啊～`；但拟声判定必须**整串都是重复单元**且单元去掉长音符/小写假名后只剩一个音，否则 `あーーーあーーーごめん` 会被吞成 `啊～`（`ごめん` 丢失）。`あ、だめ`、`はい` 这类带词的一律走翻译。
24d-6. **句子被绑到"碎语音段"上，导致字幕比台词早 1 秒出现又早 1 秒消失**（实例：`マジやばい/マジやばい/ちょっと待って` 三句，VAD 给出十几个 0.2s 碎段，第三句被绑到 1311.5 的小段，而真实说话到 1313.5）。→ ①`_group_spans()`：段数 > 句数时按**语音秒数**把连续段聚成"句数"个组，不再取前 N 个碎段；②`CUE_TAIL_PAD=0.5s`：VAD 会在气声台词尾部提前收尾，字幕末尾补 0.5s（不越过下一条）。
24e. **0.3 秒的字幕等于没有字幕**：VAD 在音乐/气声里会切出大量 0.3s 碎段，一句话被分配到 0.3s 窗口 → 屏幕上闪一下就没，观众报"没有字幕"。→ `_pad_short_cues()` 把每条 cue 补到 `MIN_CUE_DISPLAY=1.0s`，上限是下一句开始时间（不重叠）。

### 4.3 音频增益类（最新踩坑）

6. **固定 `volume=4.0` 毁音频**：t=600 那段原始 -42dB 极轻声，×4 才听得清；但片头 20s 对话原始 -18dB 正常电平，×4 后 peak 1.0、**6% 采样削波失真**，「おはようございます」听不出。→ 改**动态归一化** `loudnorm=I=-18:TP=-2:LRA=7`，轻声放大、正常电平不削波。
7. **loudnorm 早期被误判「破坏 VAD」**：那是因为 whisper.cpp 的 VAD 本身不可靠（见 #8）。改用 ffmpeg 自己做静音门禁后，loudnorm 没问题。

### 4.4 VAD 类

8. **不用 faster-whisper 内置 `vad_filter`，但要用 Silero 本体**：早期结论「Silero 不可靠」是在 `vad_filter=True` + 旧增益 + 另一片源上得到的——内置 VAD 会把真人轻声对白整段丢掉，whisper-server `--vad` 还会 exit 139。但只要**脱离这两个封装、直接跑 ONNX**（`vad_silero.py`，`silero_vad_v6.onnx`），判定非常干净：对白段 max prob 0.98、音乐段 speech 仅占 20%。→ 结论修正：**VAD 用于「这段有没有人声」的门禁是可靠的，用于「切分给 faster-whisper」不可靠**。
8b. **能量门禁会把 BGM 当人声**：`silencedetect=-42dB` 只看响度，片中的音乐/环境音全是"有声"，于是被送进 Whisper，触发温度阶梯退化，一窗烧掉 8–14s 还编出「ご視聴ありがとうございました」。→ 门禁换成 Silero（音乐 prob < 0.1），音乐窗直接 0.3s 返回 silence。
8c. **Silero ONNX 不能靠 `import faster_whisper` 复用**：它和 mlx 同时进一个进程会 `OMP Error #15: libomp already initialized` 直接 abort（exit 134）。→ 把 `silero_vad_v6.onnx` 复制进 `models/vad/`，用 `vad_silero.py` 独立实现（只依赖 onnxruntime + numpy）。
8d. **VAD 不适合"分段后逐段识别"**：尖叫/喊叫类台词的召回会掉（w800 只切出 2 个 0.5–1.1s 的段），而 <0.7s 的段又会幻觉出「ご視聴ありがとうございました」。→ VAD 只做**门禁 + 定边界**，识别仍然整窗一次做完。

### 4.2c MLX 实测类（本轮新踩，最容易误判）

23. **「偶发 7–14s」的根因是内存交换，不是 Metal shader**：一度以为是「新音频模式触发着色器重编译」。真相：16GB 机器上 llama(1.9G) + mlx(1.6G) + IINA 播放同时常驻，`vm.swapusage` 用到 6.1G/7.1G、pageouts 46 万次 → 权重被换出，MLX 从 **600 fps 掉到 45 fps**（同一段音频 14s vs 1.0s）。判据就两个：`sysctl -n vm.swapusage` 和 mlx 进度条的 `frames/s`。缓解：mlx `mx.set_cache_limit(512MB)`、llama `-c 2048`。
24. **Whisper 温度阶梯会在「短窗口 + 尾部静音」上退化**：greedy 结果 `compression_ratio` 正常，但一旦循环就飙到 **31–51**，输出 `あ、あ、あ…` / `セセセ…` / `主主主…`，一窗要跑完 0/0.2/…/1.0 六次解码 ≈ 9–15s。→ **先用 `temperature=(0.0,)` 贪心（1.0s，12 个普通对白窗口与全阶梯逐字一致），只在检测到退化时用 `(0.2, 0.4)` 有界重试**（2.5–7.5s，能把 w800 的「あっつっ!…話してどうしてるのね!」救回来）。
24b. **重复循环不一定重复同一个字**：`あらあらあら…`（两字交替）、`あーーーっあーーーっ…` 用"同字符连续"规则抓不到，`compression_ratio` 也可能只有 **2.65**（日语本身压缩率就高）。可靠信号是**词汇坍缩**：23 个字里只有 5 个不同字（去重率 0.22，正常对白 >0.5）。→ 检测四条件：cr>4 / 同字符≥6 连 / 1–5 字单元连续重复≥4 次 / 去重率<0.35（≥12 字时）。
24c. **循环会顺着 context 传染**：插件把上一句 ja 尾部当 `initial_prompt` 传回，一旦那句本身是循环，后面每一块都开始循环（实测 @6010 出 `あらあら…`，之后 3 块全是 `あらあら…`，与音频已无关）。→ bridge 侧 `_clean_context()` 拒绝循环文本，插件侧 `isSaneJa()` 不把它存进 context。
24d. **两侧都退化就丢弃，不要"挑更长的那个"**：循环文本越长越糟，而且 200 字的日语会让 3B 翻译烧 6 秒（整请求 12s → 预取掉队 → resync → 观众看不到字幕）。→ 两次都退化直接返回空。
25. **不能一刀切 `temperature=0`**：对白窗没问题，但尖叫/急促台词窗会退化成 `あ、あ、あ…` 且丢掉后面的整句。反过来默认阶梯在这些窗也是垃圾（`てててて…`）。→ 必须「贪心 + 退化检测 + 有界重试」三段式，检测信号用 `compression_ratio > 4` 或同字符连续 ≥6 次。
26. **退化窗里没有真人声时不要重试**：`speech_sec < 1.2s` 的退化基本是音乐/环境音，重试只是花 3–8s 再产一遍噪声。→ 直接返回空，字幕宁缺勿错。
27. **测延迟必须分阶段计时**：同一行日志里 `[req]` 的总耗时包含**全局锁排队时间**（`lock=`）——之前把别的请求排队 18s 记到 ASR 头上，得出「带 context 就慢」的错误结论。bridge 现在输出 `[brk] ext/vad/asr/tr/tot`，先看分解再下结论。
28. **`path_or_hf_repo` 传 repo id 会每次走网络**：mlx-whisper 每次 `transcribe()` 都调 `huggingface_hub` 解析快照，本机代理还会间歇 502（表现为硬失败）。→ 传**本地 snapshot 目录**（`~/.cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo/snapshots/<hash>`），零网络。
29. **同进程 import `faster_whisper` 会 abort**：`OMP Error #15`。→ VAD 独立实现；faster-whisper 只在单独的离线对比进程里用。

### 4.5 插件机制类

9b. **"IINA 打开文件却不自动出字幕"的真凶是首帧判定**：旧代码在第一次 tick 就判定，此时 `core.status.duration` 还是 0、`core.subtitle.tracks` 还是空数组，条件 `dur > 10` 不成立 → 走 else 分支把 `currentVideo` 标记成"已处理"，**之后永远不再检查这个文件**。→ 改成"待判定 + 静置 + 一次性决策"状态机（见 2.4）。
9c. **有字幕轨 ≠ 有字幕在显示**：IINA 是否自动选中内嵌轨取决于偏好设置；`tracks.length > 0` 但 `id = -1`/`currentTrack` 为空时，观众眼前什么都没有。→ 判定要用 `tracks.length > 0 && (id > 0 || currentTrack)`，只看轨道数会把这种情况误判成"已有字幕"。
9d. **不能用按键/AppleScript 触发 IINA 菜单**（沙盒 -10004）。让插件代码生效的办法：IINA → 设置 → 插件 → 选中本插件 → **Reload**，或直接重启 IINA（插件在启动时加载）。
9e. **`iina-plugin link` 只是符号链接，插件默认不启用**：必须 `defaults write com.colliderli.iina "PluginEnabled.com.lanca.live-ai-subs" -int 1` 后重启 IINA。
10. **overlay `simpleMode()` 必须在 `iina.window-loaded` 之后调用**，否则抛 "window is not available"。
11. **事件时机不可靠**：`iina.file-loaded` 可能在插件注册监听前就触发。→ 改为 `setInterval` 轮询 `core.status.url`。
12. **重开同文件不出字幕**：idle 分支未清 `currentVideo`；已修。
13. **`setInterval` 异步重入**：加 `tickRunning` 保护。
14. **日志每条 fork bash 太重**：批量 12 条或 3s 刷一次盘。

### 4.6 播放控制类

15. **seek 判定写死 3s 阈值**：向前拖 1–3s 漏检、16x 倍速误判。→ 用 `abs(delta - speed*dt) > max(0.7, speed*dt*0.6)`。
16. **预取落后播放头**：`nextTarget` 落后就 resync，别吐过期 cue。
17. **倍速理论上限**：ASR ~5–9s/6.5s 上下文，>3x 会稀疏，UI 提示。

### 4.7 环境与工程类

18. **沙盒限制**：`ps`/`top`/`sudo`/AppleScript 按键 均不可用（权限违例 -10004）；用 `libproc`/`footprint` 读资源，用 `kill -15 PID` + `open -a IINA` 重启。
19. **`pgrep -f` 会匹配到 shell 自身导致误杀**：用 `pgrep -x IINA` 或 lsof 按端口取 PID。
20. **`nohup cmd &` 不足以让服务活下来**：调用方（IINA 的 `utils.exec`、终端、agent 会话）退出时整个进程组会被回收，出现"services ready 后几秒服务全没"。→ 用 `tools/detach.py`（`start_new_session=True`，即 setsid）启动，子进程自成会话，实测启动方退出 8s 后服务仍在。
20c. **`TaskStop` 之后端口仍被占用**：杀掉任务后旧 python 子进程还会活几秒，立刻重启会 `OSError: [Errno 48] Address already in use` 直接退出（而管道里的 grep 让退出码显示成 0，很容易误判"启动成功"）。→ 重启前先 `lsof -nP -iTCP:18182 -sTCP:LISTEN -t` 取 PID 并 `kill`，确认端口空闲再起。
20b. **detached 进程在本 agent 沙盒里偶发 `execution attempt 不存在或已关闭`**（bridge 报 500）：这是沙盒对"发起它的工具调用已结束"的进程的限制，实测延迟 12s 后调用 ffmpeg 又是正常的，属环境噪声；用户侧由 IINA 启动不受影响。调试期需要 bridge 能稳定调 ffmpeg 时，改用后台任务持有前台进程。
21. **`iina-plugin pack` 在本环境连续 SIGTERM(137)**：开发期用 link 源码目录加载即可。
22. **下载慢**：所有下载优先国内镜像——Hugging Face 走 `hf-mirror.com`，GitHub Release 走 `gh-proxy.com`，PyPI 走阿里/USTC。大文件 `curl -L -C -` 续传，下完核对大小/哈希（详见用户级 MEMORY.md）。

---

## 5. 启动 / 停止 / 调试

### 启动服务
```bash
bash $HOME/AI/实时字幕/start_services.sh
# 幂等；等待 /health 报 whisper+llama 都 true
curl -s http://127.0.0.1:18182/health
# {"ok": true, "backend": "http", "whisper": true, "llama": true}
```
注意：本会话里 `nohup` 启动的服务会被回收，测试时用后台任务直接持有
`whisper-server`、`llama-server`、`bridge_server.py` 三个前台进程。

### 停止服务
```bash
bash $HOME/AI/实时字幕/stop_services.sh
```

### 插件加载（开发）
```bash
iina-plugin link $HOME/AI/实时字幕/live-ai-subs
defaults write com.colliderli.iina "PluginEnabled.com.lanca.live-ai-subs" -int 1
# 重启 IINA
```

### 日志与标定（改判据前必读）

| 文件 | 内容 | 用途 |
|---|---|---|
| `logs/bridge.log` | 每块 `[brk]`：分阶段耗时 + **vmax（VAD 最大概率）/ sp（判定的语音秒数）/ WEAK 标记** + 丢弃原因 + 原始日语（120 字）；每块 `[req]`：最终中文 | 定位"为什么这块没字幕" |
| `logs/films/<影片名>.log` | **可读主日志**（按影片分文件）：`当前时间  T+时:分:秒  [识别/字幕/放弃] 内容` | 直接给用户看、逐段核对 |
| `logs/drops.log` | **只记被丢弃的块**：`时间 @位置 原因 vmax=.. sp=.. ja=原始识别` | 校准主文件，扫一眼就知道丢得对不对 |
| `logs/plugin.log` | 播放器侧 `CUE/RENDER/resync`（含字幕文本） | 判断是否"识别出来了但没渲染" |
| `logs/llama.log` | 翻译服务 | 翻译异常时看 |

影片日志示例（同一个块的三段会成对出现，便于对照）：
```
2026-11-21 21:12:49  T+00:13:24.00  [识别] サイズがなかなかぴったりなサイズがないんですよね
2026-11-21 21:12:49  T+00:13:24.27  [字幕] 找不到合适的尺寸   [00:13:24.27 → 00:13:27.31]
2026-11-21 21:13:11  T+01:58:20.00  [放弃] silence — VAD 判定没有语音（静音/纯环境音）   (vmax=0.02 sp=0.00)
```

**标定命令**（不需要改代码、不重启）：
```bash
python3 tools/probe.py <video> <seconds>
```
输出：VAD 在 0.5/0.4/0.3/0.2/0.15 各阈值下的语音时长与分段、贪心识别的原文与压缩比、
**当前规则会怎么判**（silence / 命中幻觉黑名单 / 拟声 / 退化重试 / 保留）、以及把
`VAD_WEAK_MIN_PROB` 改成其它值会怎样。

**判读经验**：`drops.log` 里 `vmax` 高（>0.5）却标了 silence/weak → **误丢**，该放阈值或改规则；
`vmax < 0.18` 说明确实没语音 → 丢掉是对的。

### 调试日志
- 插件：`/tmp/liveai_debug.log`（`tick` / `CUE @` / `RENDER @` / `seek` / `resync`）。
- 服务：`logs/llama.log`、`logs/bridge.log`、`logs/whisper.log`（旧）。

### 手动验证 ASR+翻译链路
```bash
curl -s http://127.0.0.1:18182/transcribe \
  -H 'Content-Type: application/json' \
  -d '{"video":"/path/to/film.mp4","start":20.0,"duration":4.0,"context":"","language":"ja"}'
# 期望：cues 里出现「早上好」；用没测过的 start 值避免命中缓存
```

### 关于 `ASR_BACKEND` 环境变量
- 默认 `mlx`（进程内 mlx-whisper fp16，1.0s/窗口，准确率最高，**推荐**）。
- `ASR_BACKEND=http` 切回 whisper-server（:18180，~3s，已验证的稳定回退）。
- `ASR_BACKEND=faster` 切到进程内 faster-whisper（识别准、有真词级时间戳，但 CPU
  ~6s/请求，**不实时**；仅供离线/交叉验证）。

### 可调环境变量（bridge）
| 变量 | 默认 | 作用 |
|---|---|---|
| `ASR_BACKEND` | `mlx` | `mlx` / `http` / `faster` |
| `MLX_MODEL` | 本地 snapshot 目录 | 换成别的 mlx 权重（传目录，别传 repo id） |
| `VAD_THRESHOLD` | `0.5` | Silero 语音阈值，安静片源可降到 0.4 |
| `VAD_MIN_SILENCE_MS` | `300` | 超过多久静音切成两条字幕（默认 2000 会合并整场戏） |
| `VAD_WEAK_MIN_PROB` | `0.18` | 低于此值判定为环境音/静音（不推理）；高于则走"弱语音"只收拟声 |
| `VAD_WEAK_PROB` | `0.15` | 弱语音模式下定位发声段用的低阈值 |
| `MLX_CACHE_MB` | `512` | MLX Metal 缓冲上限，内存紧张就调小 |
| `MIN_CUE_DISPLAY` | `1.0` | 单条字幕最短停留秒数（不够就延长到下一句前，绝不重叠） |
| `CUE_TAIL_PAD` | `0.5` | 字幕末尾补偿秒数（VAD 常在气声台词尾部提前判结束） |

气声/拟声映射表 `VOCAL_TABLE` 在 `bridge_server.py` 顶部，可直接按喜好改（默认 あ→啊～、ん→嗯～、は→哈～、あら→哎呀～）；译出来的 `啊…` 也会被 `_normalize_interjection()` 统一成 `啊～`。
| `BRIDGE_TRACE` | `1` | 打印 `[brk] ext/vad/asr/tr/tot` 分阶段耗时 |

### 性能自检（改完必看）
```bash
curl -s http://127.0.0.1:18182/health          # mlx_ready 必须 true
sysctl -n vm.swapusage                         # used 接近 total 就是内存告急
python3 tools/bench_bridge.py <video> <start> 12 4.0   # 按插件节奏压 12 个窗口
python3 tools/audit_asr.py <video> <bridge.log> 10      # 抽 10 窗与 CT2 对准确率
node tools/sim_autostart.js                     # 假 IINA 环境回归自动接管逻辑（5 场景）
```
`bench_bridge.py` 看 `asr` 列（应 1.0–1.1s）与 `wall`（应 1.5–2.5s）；若 `wall` 远大于
分解和，说明有别的请求在抢锁（多半是插件自己正在播片）。

---

## 6. 当前状态快照（2026-11-21 晚）

- ✅ ASR 切到 **mlx-whisper fp16**（准确率第一），实测 **1.0–1.1s / 6.5s 窗口**。
- ✅ 门禁换成 **Silero VAD**（`vad_silero.py`）：音乐/环境音窗 0.3s 返回 silence，不再烧 8–14s 编假台词；同时用它的语音段做字幕边界。
- ✅ 解码策略：`temperature=(0.0,)` 贪心 → 退化检测（`compression_ratio>4` 或同字符≥6 连）→ `(0.2,0.4)` 有界重试；`speech_sec<1.2s` 的退化直接丢弃。
- ✅ 单请求 1.5–2.3s < CHUNK 4s，插件侧 `next` 稳定领先播放头 12–17s，不再持续 resync。
- ✅ 诊断能力：`[brk]` 分阶段耗时 + `lock=` 排队时间 + `ja=` 原文，`tools/bench_bridge.py`、`tools/audit_asr.py`。
- ⚠️ 已知限制：① 16GB 机器必须让 mlx + llama 共存，swap 偏高时整体会退化，先看 `vm.swapusage`；② 尖叫/急促台词窗偶发走有界重试（2.5–7.5s），靠 LEAD=12s 吸收；③ >3x 倍速字幕会稀疏。
- 📌 **两条铁律**：① 处理耗时必须 < CHUNK，否则字幕永不渲染；② 换模型/调增益/改参数后，先看插件日志
  有无 `tick exception`、CUE 是否紧跟播放头、RENDER 是否出现，再交给用户验证——本项目多次因「直接改全局参数」引入回归。
- 📌 **第三条铁律（本轮新增）**：任何「变慢了」先做**分阶段计时 + 内存检查**，不要凭直觉换模型或改温度——本轮两个"根因"（Metal 抖动、context 污染）都被实测推翻，真因是 swap 抖动和温度阶梯退化。
