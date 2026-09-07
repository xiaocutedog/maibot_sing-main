# 让麦麦说话和唱歌（maibot-sing）

## 准备更新的功能：
1. 可以让麦麦发送文件
2. 修复网易云扫码登录

---

用 **RVC 克隆音色** 翻唱歌曲和说话。内嵌音乐搜索（网易云 / QQ 音乐）与 MiMo TTS，通过一个独立的 RVC sidecar 进程完成歌声转换，实现「点歌 → 人声分离 → 换音色 → 混伴奏 → 发送语音条」的完整闭环。

- 🎤 **翻唱**：支持纯人声 / 带伴奏两种模式，伴奏与人声按原曲时间轴对齐
- 🎚️ **自动变调**：提供一段音色示例音频，按示例与人声音高差自动计算变调
- 🗣️ **说话**：MiMo TTS 合成后可选过一遍 RVC 换音色（toml 开关）
- 🔑 **音乐登录**：网易云支持账号密码自动登录与扫码登录（`/网易云音乐登录`）；QQ 为扫码登录（`/qq音乐登录`）。二维码均发到聊天里
- 🧹 **缓存自清理**：翻唱语音缓存默认保留 5 天，可配置

---

## 一、功能与用法

| 功能 | 触发方式 | 说明 |
|---|---|---|
| 🎤 翻唱（纯人声） | `/翻唱 <歌名> [-v 模型名]` 或自然语言说「我想听你唱 XX」 | 搜歌 → UVR5 分离人声 → RVC 换音色 → 发送语音条 |
| 🎶 翻唱（带伴奏） | 自然语言说「带伴奏唱 XX」「加上伴奏唱 XX」 | 人声分离 → 换音色 → 按原曲时间轴混入伴奏 |
| 🗣️ 说话 | `/说 <文本>` 或让 bot 发语音回复 | MiMo TTS 合成 → RVC 换音色（可开关）→ 发送语音条 |
| 📋 音色列表 | `/音色列表` | 列出 RVC 可用的音色模型 |
| 🔑 QQ 扫码登录 | `/qq音乐登录` | bot 发送登录二维码，手机 QQ 扫码确认后自动保存登录态（仅管理员） |
| 🔑 网易云扫码登录 | `/网易云音乐登录` | 同上，用网易云音乐 App 扫码（仅管理员） |
| 🔑 网易云密码登录 | maibot启动时自动 | 启动时用配置里的账号密码自动登录（需扫码优先时清空账号或直接扫码） |
| 🧪 网易云登录测试 | `/163logintest` | 校验登录态，成功时显示账号昵称（仅管理员） |
| 🧪 QQ 登录测试（未测试） | `/qqlogintest` | 校验登录态，成功时显示账号昵称（仅管理员） |
| 🔑 网易云 cookie 登录 | `/163cookie <MUSIC_U>` | 粘贴浏览器里的 MUSIC_U 直接登录（仅管理员，扫码被风控时的兜底） |

### 翻唱 vs 说话（工具边界）

两条链路共用 RVC 换音色，差别只在「原声」的来源：

```
【翻唱】搜歌 → 下载整曲 → UVR5 分离人声 ─┐
                                         ├─→ RVC 换音色 →（可选混伴奏）→ 发送语音条
【说话】文字 → MiMo TTS 合成原声 ────────┘
```

已做互斥声明，但可能仍存在冲突的情况，请酌情与发送音乐卡片的插件使用：

- `cover_song`（本插件）：**bot 亲自开口唱**——「我想听你唱XX」「你唱一首XX」「翻唱XX」

---

## 二、工作原理

RVC（Retrieval-based Voice Conversion，歌声转换）推理依赖 **Python 3.9** 运行时，而 MaiBot 运行在 **Python 3.12**，两者无法共用同一进程。因此本插件采用 **HTTP sidecar** 架构：

```
┌──────────────────────────────────────────────────────┐
│  MaiBot 插件 (Python 3.12)                           │
│  plugin.py / rvc_client.py / music/ / services/      │
└──────────────────────┬───────────────────────────────┘
                       │ HTTP (localhost:7898)
                       ▼
┌──────────────────────────────────────────────────────┐
│  RVC sidecar (Python 3.9，用 RVC 自带 runtime/python. |
|exe)                                                  │
│  sidecar/server.py                                   │
│    ├─ GET  /health     健康检查                       │
│    ├─ GET  /models     列出音色模型                   │
│    ├─ POST /separate   UVR5 人声分离（含前后静音裁剪） │
│    ├─ POST /convert    RVC 音色转换                   │
│    └─ POST /cover      翻唱一站式：分离 → 换音色       │
│                        →（可选裁剪静音/混伴奏/自动变调）│
└──────────────────────────────────────────────────────┘
```

sidecar 由插件自动拉起，模型在进程内做单例缓存，避免每次重复加载（几 GB 模型加载很慢）。

几个关键实现细节：

- **伴奏对齐**：混伴奏时人声保留整段（不裁剪前后静音），与伴奏按原曲时间轴逐样本混音；纯人声输出时才裁剪头尾空白。
- **防伴奏爆音**：MDX 分离的伴奏 stem 写盘前缩放回 ±1 内（防止 PCM_16 硬削波）；混音结果以 int16 落盘。
- **防重复发送**：语音发送带软截止（180s），超时后先按失败上报，后台继续观察最终结果——确认送达后补发一条文字说明；结果未知时绝不重发，杜绝语音条发两遍。
- **工具超时**：翻唱/说话工具声明了长 RPC 超时（900s/600s），避免宿主默认 60s 超时把慢转换误报为失败。

---

## 三、环境要求

| 组件 | 要求 |
|---|---|
| MaiBot | 已支持插件系统的版本（SDK ≥ 2.5.1） |
| RVC | 本地完整安装（含 `runtime/python.exe` 3.9 运行时、hubert/rmvpe 特征文件） （测试环境为RVC20240604Nvidia） |
| UVR5 | 本地完整安装（含人声分离模型文件） （测试环境为UVR5+Kim Vocal 1） |
| GPU | 推荐 N 卡（RVC 推理走 CUDA）；无 GPU 可 CPU 跑但很慢 |
| 网络 | 音乐搜索（网易云无需登录也可搜；QQ 搜索需登录态）、说话功能需访问 MiMo API |

**RVC 与 UVR 根目录需包含以下内容：**

```
RVC/
├── runtime/python.exe          # Python 3.9 运行时（关键）
├── ffmpeg.exe                  # 音频处理（sidecar 会自动加入 PATH）
├── assets/
│   ├── weights/*.pth           # 已训练的音色模型
│   ├── uvr5_weights/*.pth      # 或使用单独的UVR
│   ├── hubert/hubert_base.pt   # 特征提取模型
│   └── rmvpe/rmvpe.pt          # 音高提取模型（用 rmvpe 算法时）
└── logs/                       # 特征检索索引（.index，可选，提升相似度）

Ultimate Vocal Remover/
├── models/*.pth      # UVR5 人声分离模型
```

---

## 四、部署步骤

### 1. 放置插件

将 `maibot_sing-main` 目录放入 MaiBot 的 `plugins/` 目录下（目录名可改，无强制要求）。

### 2. 安装依赖

插件依赖 `aiohttp`、`httpx`、`cryptography`（网易云 eapi/weapi 加密）、`segno`（扫码登录二维码本地渲染）：

```bash
pip install aiohttp httpx cryptography segno
```

> sidecar 本身用 RVC 自带运行时，**无需**在 sidecar 侧安装额外依赖。

### 3. 生成并填写配置

插件首次加载时会自动从 `config.example.toml` 复制生成 `config.toml`（不会覆盖已有配置）。编辑 `config.toml`：

```toml
[rvc]
rvc_root = "D:/RVC20240604Nvidia"     # ★ 改成你的 RVC 根目录（可迁移关键）
python_path = ""                       # 留空自动用 {rvc_root}/runtime/python.exe
port = 7898                            # sidecar 端口，避开 RVC WebUI 的 7897
auto_start = true                      # 插件加载时自动拉起 sidecar
default_model = "LuoXiaohei_48.pth"    # ★ 默认音色（assets/weights 下的 .pth 文件名）
f0_method = "rmvpe"                    # 音高提取算法：pm/harvest/crepe/rmvpe

[mimo]
api_key = "sk-..."                     # ★ 说话功能需要，你的 MiMo key
voice_mode = "preset"                  # preset 预置音色 / clone 参考音频复刻

[music]
netease_account = ""                   # 网易云账号（手机号/邮箱），填了自动密码登录
netease_password = ""
```

> 只想用翻唱功能，可先不填 `mimo.api_key`；说话功能（`/说`）依赖 MiMo TTS。

### 4. 加载插件并验证

在 MaiBot 中启用插件，加载后会自动：拉起 sidecar → 清理过期语音缓存 → 恢复/建立音乐平台登录态。

```bash
# 健康检查
curl http://127.0.0.1:7898/health
# → {"status": "ready", "device": "cuda:0"}

# 查看音色模型
curl http://127.0.0.1:7898/models
```

然后在聊天中测试：

```
/音色列表
/翻唱 晴天 -v LuoXiaohei_48.pth
带伴奏唱 起风了
/说 你好呀
/qq音乐登录
```

---

## 五、配置说明

### `[rvc]` — RVC 声音转换（可迁移核心）

| 字段 | 说明 |
|---|---|
| `rvc_root` | RVC 安装根目录，**迁移时只需改这一处** |
| `python_path` | RVC Python 解释器，留空自动用 `{rvc_root}/runtime/python.exe` |
| `port` | sidecar 端口，默认 7898（避开 WebUI 7897） |
| `auto_start` | 插件加载时自动拉起 sidecar |
| `default_model` | 默认音色模型文件名（含 `.pth`） |
| `f0_method` | 音高提取算法：`rmvpe` 效果最好；`pm` 快；`harvest` 低音好但慢；`crepe` 效果好但吃 GPU |
| `f0_up_key` | 默认变调（半音数，升 12 = 升八度，降 8 度 = -12） |
| `model_keys` | 按模型自动变调映射：模型文件名 → 半音数。**优先级最高**（手动覆盖） |
| `auto_key` | 自动变调开关：按示例音频与歌曲人声音高差自动计算变调（`model_keys` 命中时不生效）。**带伴奏时伴奏会跟随人声变调相同的半音数**（时长不变），避免整首歌跑调 |
| `auto_key_offset` | 自动变调微调（半音）：在自动计算结果上额外升/降，正=升、负=降，默认 0 |
| `auto_key_max` | 自动变调幅度上限（半音绝对值，默认 5）：变调过大音色会发哑，高音发哑就调低（如 3） |
| `sample_audio` | 音色示例音频路径（该音色的唱歌片段，作为自动变调基准音高；建议 5~30 秒清唱） |
| `index_rate` | 检索特征占比 0~1，越大越像目标音色 |
| `filter_radius` | harvest 中值滤波半径 |
| `resample_sr` | 后处理重采样，0 = 用模型原生采样率 |
| `rms_mix_rate` | 音量包络融合比例 |
| `protect` | 清辅音保护 0~0.5 |
| `uvr_model` | UVR5 人声分离模型（支持 MDX-Net onnx 与 VR 架构） |
| `uvr_agg` | 人声提取激进程度 0~20 |
| `uvr_weights_dir` | 外部 UVR5 权重目录（如 Ultimate Vocal Remover 的模型目录），留空用 RVC 自带 |
| `voice_cache_dir` | 语音条本地缓存目录，留空用插件运行时目录 |
| `voice_cache_retention_days` | 翻唱语音缓存保留天数（按文件修改时间，0=永久保留），默认 5。bot 启动、配置热更新、每 24 小时各清理一次 |

### 变调的三层优先级

```
model_keys 手动映射  >  auto_key 自动变调  >  f0_up_key 默认值
```

自动变调原理：sidecar 用 RMVPE 分别估计「示例音频」与「歌曲分离人声」有声部分的中位基频，按
`半音数 = round(12 × log2(示例音高 / 歌声音高)) + auto_key_offset` 计算，限制在 ±12 半音内，日志会打印两个频率。
示例音频建议用目标音色的**清唱片段**（不要带伴奏，否则基准会混入伴奏音高）。

带伴奏模式下，伴奏会跟随人声做相同半音数的变调（变调不移速，时长不变），
保证人声与伴奏的调性一致。

### `[mimo]` — MiMo TTS（说话基础 TTS）

| 字段 | 说明 |
|---|---|
| `api_key` | MiMo API Key |
| `api_base_url` | MiMo API 地址 |
| `voice_mode` | `preset` 预置音色 / `clone` 参考音频复刻 |
| `preset_voice` | 预置音色 ID（仅 `preset` 模式生效） |
| `reference_audio` | `clone` 模式下的参考音频**绝对路径**（建议 ≥30 秒、无噪音） |
| `rvc_after_tts` | 说话是否在 TTS 后再过一遍 RVC 换音色；`false` 直接发送 TTS 原声。默认 `true` |

> `clone` 模式的参考音频只影响喂给 RVC 的「原声」；只要 `rvc_after_tts = true`，最终音色由 RVC 决定。

### `[music]` — 音乐搜索与登录

| 字段 | 说明 |
|---|---|
| `default_platform` | 默认平台：`163`（网易云）/ `qq`（QQ音乐） |
| `search_limit` | 搜索结果数量 |
| `netease_account` / `netease_password` | 网易云账号密码（手机号或邮箱），填写后插件加载时自动登录并缓存登录态 |
| `netease_countrycode` | 手机号区号，默认 `86` |
| `netease_music_u` / `netease_csrf` | 网易云 cookie 回退（优先级低于账号密码登录） |
| `qq_uin` / `qq_key` | QQ 音乐 cookie 回退；推荐用 `/qq音乐登录` 扫码代替 |

登录态说明：

- **网易云**：支持 weapi 账号密码登录。登录成功后 cookie（MUSIC_U/__csrf）缓存到插件运行时目录 `music_login_cache.json`，重启不重复登录。遇到网易云风控要求二次验证时（如 code 803）会明确报错，日志会明确报错并退回 cookie 方式。
- **QQ 音乐**：腾讯风控不支持无浏览器的密码登录，提供**扫码登录**：发 `/qq音乐登录`，bot 把二维码发到聊天里，手机 QQ 扫码并在手机上确认即可，登录态（uin / qqmusic_key）自动缓存。
- **扫码指令均为管理员限定**（`permission="operator"`）：仅本地控制台与 `bot_config.toml` 中 `[plugin] permission` 列表内的用户可触发。
- **登录测试**：`/163logintest`、`/qqlogintest` 校验当前登录态，成功只显示账号昵称（不显示账号 ID）。两条命令声明为 `permission="operator"`，仅本地控制台与 `bot_config.toml` 中 `[plugin] permission` 列表内的用户可触发（格式如 `qq:123456789`）。
- 登录态缓存文件包含账号凭证，**不要外传**。

### `[components]` — 组件开关

| 字段 | 说明 |
|---|---|
| `command_enabled` | 是否启用命令（`/翻唱`、`/说`、`/音色列表`、`/qq音乐登录`、`/网易云音乐登录`、`/163cookie`、`/163logintest`、`/qqlogintest`） |
| `tool_enabled` | 是否启用 LLM 自主触发的工具（`cover_song`、`speak_voice`） |

---

## 六、手动启动 sidecar（可选）

默认 `auto_start = true` 由插件自动拉起。若想独立调试，可手动启动：

```bash
# Windows
D:/RVC20240604Nvidia/runtime/python.exe sidecar/server.py --rvc-root D:/RVC20240604Nvidia --port 7898

# Linux / macOS（路径按实际调整）
./runtime/python.exe sidecar/server.py --rvc-root /path/to/RVC --port 7898
```

手动启动后，插件会探测到端口已有服务并直接复用，不会重复拉起。

> ℹ️ **代码更新后无需手动重启 sidecar**：sidecar 的 `/health` 会汇报代码版本与 PID，插件启动及配置重载时发现端口上是旧版本残留进程，会自动终止并重新拉起；每次翻唱/说话前也会自动探活，进程崩溃后被拉起。若手动调试，仍可按上述命令独立启动。

---

## 七、常见问题

**Q：带伴奏翻唱时人声和伴奏对不齐？**
已修复：混伴奏模式下人声保留整段、与伴奏按原曲时间轴逐样本混音。若仍复现，请确认 sidecar 已重启（见上文第六节）。

**Q：翻唱/说话为什么 bot 说「调用失败」但语音后来发出来了？**
已修复：工具已声明长 RPC 超时（翻唱 900s / 说话 600s），宿主会等完整流程结束再上报结果。

**Q：同一条语音会发两遍吗？**
已修复：发送超时后不会盲目重发，而是后台观察最终结果；只有宿主明确返回「未发出」才会尝试备用发送方式。

**Q：说话为什么听起来不像目标音色？**
说话链路是「TTS 原声 → RVC 换音色」，RVC 对说话语料的转换效果弱于唱歌。可尝试调整 `index_rate`、`protect`，或换一个针对该音色训练得更好的模型。

**Q：搜索 QQ 音乐报「需要登录」？**
QQ 音乐搜索/取链需要登录态：发 `/qq音乐登录` 扫码，或在配置里填 `qq_uin` + `qq_key`。网易云搜索无需登录，登录后可获得更高音质。

**Q：sidecar 启动失败？**
检查 `rvc_root` 路径是否正确、`runtime/python.exe` 是否存在、端口 7898 是否被占用（日志会给出明确报错）。另外翻唱/说话都需要 GPU 显存，显存被其他任务占满时会报 ONNX CUDA 错误。

**Q：语音缓存会无限增长吗？**
不会。默认保留 5 天，超期自动清理；`voice_cache_retention_days` 可调，设 0 则永久保留。

---

## 八、依赖

- **MaiBot 插件侧（Python 3.12）**：`aiohttp`、`httpx`、`cryptography`、`segno`
- **RVC sidecar 侧（Python 3.9）**：复用 RVC 自带运行时，无额外依赖

---

## 九、目录结构

```
maibot_sing-main/
├── _manifest.json         # 插件清单（含 send.text/custom/image 能力声明）
├── plugin.py              # 主入口：命令 / Tool / 生命周期 / 登录编排 / 缓存清理
├── config.example.toml    # 配置模板
├── requirements.txt       # 插件依赖
├── README.md
├── rvc_client.py          # sidecar HTTP 客户端
├── music/
│   └── search.py          # 网易云/QQ 音乐搜索、取链、账号密码登录、QQ 扫码登录
├── services/
│   ├── mimo_tts.py        # MiMo TTS（说话基础 TTS）
│   └── pipeline.py        # 编排：搜歌/TTS → 分离/转换/混伴奏 → 发送
└── sidecar/
    └── server.py          # RVC sidecar（Python 3.9）：分离 / 转换 / 混伴奏 / 自动变调
```

## 特别鸣谢

本项目在开发过程中使用了以下优秀的开源项目，特此致谢：

- **[ling-tts-bot](https://github.com/Ling-LA/ling-tts-bot)** —— MaiBot 的 Xiaomi MiMo v2.5 音色克隆语音回复插件。

- **[maibot-music](https://github.com/pan-ice/maibot-music)** —— MaiBot 音乐插件，支持搜索点歌、解析音乐链接、发送语音音频。

- **[Retrieval-based-Voice-Conversion-WebUI (RVC)](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI)** —— 简单易用的语音音色转换/变声器框架，支持用少量语音数据快速训练高质量音色转换模型。

- **[Ultimate Vocal Remover (UVR5)](https://github.com/leebufan/Ultimate-Vocal-Remover)** —— 基于深度神经网络的开源人声伴奏分离工具，是目前最优秀的人声分离工具之一。