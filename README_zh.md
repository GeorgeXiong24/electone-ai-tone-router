# electone-ai-tone-router

> 语言 / Language：[English](README.md)

给一个歌名（或链接），在你的 **雅马哈 ELB-02（STAGEA）** 上得到一套可演奏的配置。
项目会下载音频、在本地把混音分离成各乐器轨道、分析哪个乐器负责主旋律/和弦/副旋律，
并把每个声部映射到 Cantabile 里的 VST 音色，每个音色对应一个键盘（右手/左手/脚键盘）。

## 当前进度

| 阶段 | 状态 |
| --- | --- |
| 音频下载（曲名 → wav） | 已完成 |
| 轨道分离（6 轨，本地 GPU） | 已完成 |
| 分离失效检测 | 已完成 |
| 主旋律 / 和弦 / 副旋律分析 | 规划中 |
| 乐器 → VST 音色映射（sforzando / Surge XT / Decent Sampler） | 规划中 |
| 生成 Cantabile（ELB-02）配置 | 规划中 |

## 实现原理

1. **下载** —— `scripts/download-audio.ps1` 用曲名搜索 YouTube（或直接用 URL），
   把 44.1kHz / 16-bit 立体声 wav 存到工作区。视频标题会被压缩成简短的
   `歌手 - 曲名`（例如 `周杰倫 - 晴天`）。
2. **分离** —— `scripts/separate-stems.ps1` 在本地 GPU 上跑 BS-RoFormer-SW，
   把混音分成 `vocals`、`drums`、`bass`、`guitar`、`piano`、`other`（外加 `instrumental`）。
   输出 32-bit float wav，分离后自动打印每轨可用性报告。
3. **检测** —— `scripts/check-stems.py` 逐窗口扫描分离结果并报告失效段落
   （见[分离的局限](#分离的局限)）。
4. **分析**（规划中）—— 判定哪个乐器是主旋律、哪些是和弦/副旋律、哪个是低音线条。
5. **映射与配置**（规划中）—— 为每个声部挑选对应的 VST 音色，并生成 Cantabile 配置：
   右手键盘 = 主旋律，左手键盘 = 和弦/副旋律，脚键盘 = 低音。
6. **演奏** —— ELB-02 只当 MIDI 控制器，电脑发声并通过 AUX IN 回到琴的音箱。
   硬件接线见 [prerequisites_zh.md](prerequisites_zh.md) / [prerequisites.md](prerequisites.md)。

注意：分离出来的是**乐器轨**，不是音乐角色；"主旋律 vs 和弦"由分析步骤判定，而不是分离器。

## 分离的局限

在拿分离结果做后续开发之前，请先读这一节。以下结论实测于三首参考曲目
（韋禮安《女孩》、周杰倫《晴天》、周杰倫《不能說的祕密》）：

- **`other` 不是乐器轨**，它是残差桶——按定义
  `other = 混音 − (vocals + drums + bass + guitar + piano)`。里面装着铜管、pad、弦乐
  以及其他模型无法归类的成分。**不要把 `other.wav` 当作"钢琴轨"或任何单一音色。**
- **即使原曲有钢琴，`piano` 轨也可能是空的。** 《晴天》全曲 piano 轨静音
  （31 个 10 秒窗口中 0 个高于 −40 dB），而独奏钢琴前奏落在了 `other` 里。
  同一模型在《不能說的祕密》上则分出了可用的钢琴（33 个窗口中有 28 个可用）。
  **准确率与曲目相关，依赖某个轨之前请先验证。**
- **稀疏前奏可能整体失效。** 三首歌的开头段落都出现 `corr(other, mix) ≈ 1.000`，
  也就是模型没有分离，而是把整段混音原样塞进了 `other`。
  `check-stems.py` 会把这类段落标为 `dumped` / `silent-melodic` 并给出时间戳。
- **vocals、drums、bass、guitar 是可靠的四个轨。** 在 6×6 往返标签测试中这四轨
  全部正确回到各自分支，且在不同曲目间电平表现稳定。

所有轨相加始终能还原混音（`corr ≈ 0.9998`，残差 ≈ −55 dB），所以数据没有丢失——
但"文件里有内容"并不等于"分离正确"。

## 环境要求

- Windows 10/11、Python 3.10+、Git
- 推荐 NVIDIA GPU（CUDA），没有可用 `-Cpu` 回退
- 约 5GB 磁盘空间（虚拟环境 + 模型权重）

## 安装

```powershell
.\scripts\setup-env.ps1
```

会创建 `.venv`、安装 CUDA 版 PyTorch 和 `bs-roformer-infer`。分离模型（约 667MB）
在首次运行时自动下载到 `<工作区>\models\`。

`tools/` 目录需要有 `yt-dlp.exe`、`ffmpeg.exe`、`ffprobe.exe`（二进制不入库，脚本从这里查找）。

## 使用方法

### 一键运行

双击 `download-audio.cmd`，依次输入曲名（或 URL）和工作区目录，下载和分离会自动接续执行。

### 下载

```powershell
.\scripts\download-audio.ps1 "女孩 - 韦礼安"
.\scripts\download-audio.ps1 "https://www.youtube.com/watch?v=..."
.\scripts\download-audio.ps1 "女孩 - 韦礼安" -CookiesFromBrowser chrome
.\scripts\download-audio.ps1 "曲名" -Workspace D:\electone
.\scripts\download-audio.ps1 "曲名" -OutDir D:\music
.\scripts\download-audio.ps1 "曲名" -NoSeparate
```

- 输入曲名会自动用 YouTube 搜索取第一个结果；B 站等站点可直接贴 URL。
- 输出固定为 wav（PCM 16-bit、44.1kHz、立体声），可直接用于分离。
- 下载完成后自动分离；加 `-NoSeparate` 跳过。

### 分离

```powershell
.\scripts\separate-stems.ps1
.\scripts\separate-stems.ps1 "D:\music\song.wav"
.\scripts\separate-stems.ps1 "song.wav" -Cpu
```

- 模型：BS-RoFormer-SW，6 轨，32-bit float wav，GPU 加速。
- 结果：`<工作区>\stems\<歌曲名>\{vocals,drums,bass,guitar,piano,other}.wav`（另有 `instrumental`）。
- 分离后自动执行质量检测，包含失效段落报告。

### 质量检测

```powershell
.\.venv\Scripts\python.exe .\scripts\check-stems.py --stems "workspace\stems\周杰倫 - 晴天" --mix "workspace\audio\周杰倫 - 晴天.wav"
```

按 10 秒窗口标记两种失效模式：

- `dumped` —— 模型没有分离，而是把整段混音原样塞进了 `other`
- `silent-melodic` —— 混音有声音，但所有旋律轨都接近静音

同时输出每轨可用率（`EMPTY` / `SPARSE`），空的 `piano.wav` 会被明确标出而不是静默通过。
该检测已接入分离脚本；各指标的含义见[分离的局限](#分离的局限)。

### 工作区

所有步骤共用一个工作区根目录，优先级：

1. `-Workspace` 参数
2. 环境变量 `ELECTONE_WORKSPACE`
3. 项目根目录的 `config.json`（交互式选择路径后回答 `y` 自动写入）
4. 默认 `<项目根目录>\workspace`

目录结构：`audio\`（下载）、`stems\`（分离结果）、`analysis\`（规划中）、
`models\`（模型缓存）。`config.json` 记录本机路径，已在 `.gitignore` 中忽略。

## 调用的开源资源

| 资源 | 用途 | 许可证 |
| --- | --- | --- |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | 音频下载 | Unlicense |
| [FFmpeg](https://ffmpeg.org/)（gyan.dev 构建） | 音频转换 | LGPL/GPL |
| [PyTorch](https://pytorch.org/) | 模型运行时（CUDA） | BSD-3 |
| [BS-RoFormer](https://github.com/lucidrains/BS-RoFormer)（lucidrains） | 分离模型架构 | MIT |
| [bs-roformer-infer](https://github.com/openmirlab/bs-roformer-infer)（openmirlab） | 推理 CLI / 模型仓库 | MIT |
| BS-RoFormer-SW 权重（jarredou，enerjazzer 镜像） | 6 轨分离权重 | 未标注——个人使用没问题，商用请自行确认 |
| [Surge XT](https://surge-synthesizer.github.io/)（规划中） | 免费合成器 | GPLv3 |
| sforzando / Decent Sampler（规划中） | SFZ / DSP 采样播放器 | 免费软件 |
| [Cantabile](https://www.cantabilesoftware.com/)（规划中） | VST 宿主（Lite 免费） | 商业软件 |

## 注意事项与限制

- 分离质量的上限取决于源音频：尽量用官方音源或高码率版本，现场版和低码率翻录会明显拖低效果。
- 密集编曲（管弦、日系流行）分离效果不如稀疏编曲；无法归类的成分会进 `other`。
- 钢琴是模型中分离能力最弱的一轨，且表现与曲目相关——详见[分离的局限](#分离的局限)。
- 从视频平台下载音频可能违反其服务条款，仅供个人使用。
- ELB-02 无法加载外部音色，电脑是音源（详见前置条件文档）。

## 后续计划

- 从分离轨判定主旋律 / 和弦 / 副旋律角色
- 乐器 → VST 音色映射表（SFZ 采样库、Surge XT 预设）
- 自动生成 Cantabile 的 rack/song 配置（Performer triggers / Network Server）
- ELB-02（右手/左手/脚）→ Cantabile 的实时 MIDI 路由
- 分离失效段落的兜底方案（目前只报告，不修复）

## 相关文档

- [prerequisites_zh.md](prerequisites_zh.md) —— 把 ELB-02 当作 MIDI 控制器（中文）
- [prerequisites.md](prerequisites.md) —— English
