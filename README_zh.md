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
| 二级细分（鼓 → 6 个部件） | 已完成 |
| 实时进度显示 | 已完成 |
| 主旋律 / 和弦 / 副旋律分析 | 已完成 |
| 乐器 → VST 音色映射（sforzando / Surge XT / Decent Sampler） | 已完成 |
| 生成 Cantabile（ELB-02）配置 | 规划中 |

## 实现原理

1. **下载** —— `scripts/download-audio.ps1` 用曲名搜索 YouTube（或直接用 URL），
   把 44.1kHz / 16-bit 立体声 wav 存到工作区。视频标题会被压缩成简短的
   `歌手 - 曲名`（例如 `周杰倫 - 晴天`）。
2. **分离** —— `scripts/separate-stems.ps1` 在本地 GPU 上跑 BS-RoFormer-SW，
   把混音分成 `vocals`、`drums`、`bass`、`guitar`、`piano`、`other`（外加 `instrumental`）。
   输出 32-bit float wav。
3. **检测** —— `scripts/check-stems.py` 逐窗口扫描**模型的原始输出**并报告失效段落
   （见[分离的局限](#分离的局限)）。
4. **细分**（可选）—— `scripts/refine-stems.py` 只对真正有声音的轨运行专用模型，
   在父轨旁生成更细的音色。目前支持 drums → `kick` / `snare` / `toms` / `hh` / `ride` / `crash`。
5. **分析**（规划中）—— 判定哪个乐器是主旋律、哪些是和弦/副旋律、哪个是低音线条。
6. **映射与配置**（规划中）—— 为每个声部挑选对应的 VST 音色，并生成 Cantabile 配置：
   右手键盘 = 主旋律，左手键盘 = 和弦/副旋律，脚键盘 = 低音。
7. **演奏** —— ELB-02 只当 MIDI 控制器，电脑发声并通过 AUX IN 回到琴的音箱。
   硬件接线见 [prerequisites_zh.md](prerequisites_zh.md) / [prerequisites.md](prerequisites.md)。

注意：分离出来的是**乐器轨**，不是音乐角色；"主旋律 vs 和弦"由分析步骤判定，而不是分离器。

每个阶段都会打印编号、实时百分比和已耗时，运行慢和卡死一眼可辨。
详见[进度显示](#进度显示)。

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
- **钢琴无法进一步细分。** 细分用的是 `audio-separator` 的模型库，其中**没有**可免费下载的
  piano / keys / organ 专用模型——MVSep 的 keys 模型是付费服务。目前唯一接好的专用模型是
  DrumSep，所以鼓组能拆成 6 个名字，而 piano 仍然只有一条轨。

所有轨相加始终能还原混音（`corr ≈ 0.9998`，残差 ≈ −55 dB），所以数据没有丢失——
但"文件里有内容"并不等于"分离正确"。

## 进度显示

长耗时阶段原本和崩溃看起来一模一样。`scripts/progress.py` 包装每个子进程并打印：

```
================================================================
[1/2] 分离轨道 / separating stems
================================================================
  分离轨道 / separating stems | elapsed 00:11 | [--------------------]   5%
  分离轨道 / separating stems | elapsed 00:31 | [##########----------]  50%
  分离轨道 / separating stems | elapsed 00:54 | [####################] 100%
  done: 分离轨道 / separating stems in 00:56
```

- 百分比来自工具自身的输出（`Estimated time remaining`、tqdm 计数），不是估算值。
- 静默超过 5 秒会显示 `working...` 心跳，能区分"慢"和"卡死"。
- 失败时打印 `FAILED: <阶段> (exit N) after <耗时>`，不会静默退出。
- 阶段编号随入口自适应：下载 → 分离 → 检测是 `[1/3]`…`[3/3]`；
  单独跑分离是 `[1/2]`…`[2/2]`。
- 输出重定向到文件时，状态行按"百分比跨度 ≥10% 或每 3 秒"节流。

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

同时会安装 `audio-separator`，它只用于可选的细分阶段。该包锁定了一些较旧的依赖版本，
直接安装会破坏 `bs-roformer`，所以脚本用 `--no-deps` 安装并显式补上需要的组件——
**不要"顺手修好"成普通安装**。

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
.\scripts\separate-stems.ps1 "song.wav" -Clean      # 先丢弃旧结果
```

- 模型：BS-RoFormer-SW，6 轨，32-bit float wav，GPU 加速。
- 结果：`<工作区>\stems\<歌曲名>\{vocals,drums,bass,guitar,piano,other}.wav`（另有 `instrumental`）。
- 分离后自动执行质量检测，包含失效段落报告。
- `-Clean` 只在**成功运行之后**才删除旧文件，所以失败或显存不足的 run
  不会把一首歌的 stem 目录清空。

### 细分（更细的音色）

```powershell
.\.venv\Scripts\python.exe .\scripts\refine-stems.py --latest
.\.venv\Scripts\python.exe .\scripts\refine-stems.py --stems "workspace\stems\周杰倫 - 晴天"
.\.venv\Scripts\python.exe .\scripts\refine-stems.py --latest --dry-run
```

只对真正有声音的轨运行专用模型，结果写到 `<歌曲名>\refined\`。
在《不能說的祕密》上得到 7 条基础轨 + 6 个鼓件，其中 8 个在可听阈值以上。
某个专用模型下载失败会被跳过并提示，**不会丢弃**其他模型已产出的结果。

这取代了早先"按响度裁到 N 轨"的做法——那种做法只是改了个响度阈值，无法产生新音色。

### 质量检测

```powershell
.\.venv\Scripts\python.exe .\scripts\check-stems.py --stems "workspace\stems\周杰倫 - 晴天" --mix "workspace\audio\周杰倫 - 晴天.wav"
```

按 10 秒窗口标记失效模式：

- `dumped` —— 模型没有分离，而是把整段混音原样塞进了 `other`
- `energy-loss` —— 各轨之和远低于混音，说明有内容被丢掉
- `all-silent` —— 混音有声音，但所有轨都接近静音

同时输出每轨可用率（`EMPTY` / `SPARSE`），空的 `piano.wav` 会被明确标出而不是静默通过。
轨集合是从目录里动态发现的，所以 2 轨、4 轨、6 轨、53 轨模型都能用；
`instrumental` 不参与重建求和，因为它是派生出来的（`混音 − vocals`），否则会被重复计算。

检测在分离脚本里针对**模型的原始输出**运行，早于任何剪枝，
这样被折叠的轨不会让 `dumped` 检测误报。各指标的含义见[分离的局限](#分离的局限)。

### 音色分析与生成

```powershell
.\.venv\Scripts\python.exe .\scripts\analyze-tones.py --song 不能說的祕密
.\.venv\Scripts\python.exe .\scripts\build-tones.py  --song 不能說的祕密
```

- `analyze-tones.py` 逐轨识别乐器、音色特点（亮度、音区、谐波、同时音数）并按 5 秒窗口
  判定音乐角色（主旋律 / 副旋律 / 和弦 / 伴奏 / 和声铺底 / 低音线条 / 节奏）。
  输出 `workspace\analysis\<歌曲>\analysis.json` 和可读的 `analysis.md`。
- `build-tones.py` 把分析结果生成**单乐器**音色文件，写到
  `workspace\tone\<右手|左手|脚>\<插件>\`——一个文件绝不混合多个乐器。
  右手 = 主旋律，左手 = 副旋律/和弦/伴奏，脚 = 低音线条；鼓组不生成音色文件
  （节奏由 ELB-02 自带 rhythm 处理）。
- 钢琴走采样音源（sforzando + Accurate-Salamander 大钢琴 SFZ）：SFZ 配置放在项目里，
  以绝对路径引用采样库。
- Surge 音色默认生成为 `.fxp` —— Surge 原生补丁格式，**拖进 Surge XT 窗口即可加载**
  （Surge 的拖放白名单包含 `.fxp`）。
  需要给 VST3 宿主（Cantabile）用时加 `--vstpreset`，会额外生成 `.vstpreset`；
  它只能由宿主自己的预设机制加载，不是 Surge 的拖放格式。
- **要"像原曲"，用采样音色**：Surge XT 是合成器、没有采样播放，工厂预设无法复现
  人声/吉他/贝斯/弦乐的真实音色。`build-sample-tones.py` 从每条 stem 里切出真实单音，
  生成 sforzando 的 `.sfz` + 对应 `.wav` 样本（每个 `.sfz` 及其 `samples/` 目录只含一件乐器）：

  ```powershell
  .\.venv\Scripts\python.exe .\scripts\build-sample-tones.py --latest
  ```

  输出到 `workspace\tone\<右手|左手|脚>\sforzando\`，并在 `tone\sample-instruments.json`
  记录每个音色的音符数与音域。钢琴不使用轨道切片，仍用 Salamander 大钢琴采样库。
- **绝不写入任何插件的默认/用户音色目录**——脚本会拒绝写到
  `workspace\tone\` 之外。`tone\manifest.json` 记录每次生成的文件清单和加载方式。

### 工作区

所有步骤共用一个工作区根目录，优先级：

1. `-Workspace` 参数
2. 环境变量 `ELECTONE_WORKSPACE`
3. 项目根目录的 `config.json`（交互式选择路径后回答 `y` 自动写入）
4. 默认 `<项目根目录>\workspace`

目录结构：`audio\`（下载）、`stems\`（分离结果）、`stems\<歌曲名>\refined\`（可选的二级细分）、
`models\`（模型缓存）。`config.json` 记录本机路径，已在 `.gitignore` 中忽略。

### 模型选择

`-Model` 可以指定 `bs-roformer-infer` 模型库中的任意模型（34 个）。默认的
`roformer-model-bs-roformer-sw-by-jarredou` 是唯一的 6 轨乐器模型，也是细分阶段所依赖的。
库里还有一个 53 轨 "mega" 模型，但它在消费级显卡上**不实用**——在 8GB 笔记本显卡上
处理 80 秒音频要 20 分钟以上，而且需要 `scripts/align-model-config.py` 修复它的配置
（分离脚本会自动处理）。推荐用 6 轨模型 + 细分。

## 调用的开源资源

| 资源 | 用途 | 许可证 |
| --- | --- | --- |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | 音频下载 | Unlicense |
| [FFmpeg](https://ffmpeg.org/)（gyan.dev 构建） | 音频转换 | LGPL/GPL |
| [PyTorch](https://pytorch.org/) | 模型运行时（CUDA） | BSD-3 |
| [BS-RoFormer](https://github.com/lucidrains/BS-RoFormer)（lucidrains） | 分离模型架构 | MIT |
| [bs-roformer-infer](https://github.com/openmirlab/bs-roformer-infer)（openmirlab） | 推理 CLI / 模型仓库 | MIT |
| BS-RoFormer-SW 权重（jarredou，enerjazzer 镜像） | 6 轨分离权重 | 未标注——个人使用没问题，商用请自行确认 |
| [audio-separator](https://github.com/nomadkaraoke/python-audio-separator) | 专用细分模型（也支持 MDX23C / MelRoformer / SCNet） | MIT |
| MDX23C DrumSep（aufr33、jarredou） | drums → kick/snare/toms/hh/ride/crash | 见模型仓库 |
| [Surge XT](https://surge-synthesizer.github.io/)（规划中） | 免费合成器 | GPLv3 |
| sforzando / Decent Sampler（规划中） | SFZ / DSP 采样播放器 | 免费软件 |
| [Cantabile](https://www.cantabilesoftware.com/)（规划中） | VST 宿主（Lite 免费） | 商业软件 |

## 注意事项与限制

- 分离质量的上限取决于源音频：尽量用官方音源或高码率版本，现场版和低码率翻录会明显拖低效果。
- 密集编曲（管弦、日系流行）分离效果不如稀疏编曲；无法归类的成分会进 `other`。
- 钢琴是模型中分离能力最弱的一轨，且表现与曲目相关——详见[分离的局限](#分离的局限)。
- 本机 DrumSep 走 CPU：`onnxruntime-gpu` 需要 CUDA 13，而 PyTorch 带的是 CUDA 12，
  所以细分阶段回退到 CPU（5 分钟的歌约 40 秒）。结果正确，但不算快。
- YouTube 偶尔会对自动下载返回 "Sign in to confirm you're not a bot"。
  加 `-CookiesFromBrowser chrome` 复用浏览器会话即可。
- 从视频平台下载音频可能违反其服务条款，仅供个人使用。
- ELB-02 无法加载外部音色，电脑是音源（详见前置条件文档）。

## 脚本一览

| 脚本 | 用途 |
| --- | --- |
| `download-audio.ps1` | 曲名/URL → wav，然后交给分离 |
| `separate-stems.ps1` | 6 轨分离，随后质量检测 |
| `check-stems.py` | 逐窗口失效检测、每轨可用率 |
| `refine-stems.py` | 专用二级细分（鼓 → 6 个部件） |
| `analyze-tones.py` | 逐轨乐器/音色特点/角色分析 → JSON + MD 报告 |
| `build-tones.py` | 生成 Surge XT 单乐器音色（`.fxp`；`--vstpreset` 可选） |
| `build-sample-tones.py` | 从每条 stem 提取单音，生成 sforzando 采样音色（`.sfz` + `.wav`） |
| `prune-stems.py` | 保留最响的 N 轨，其余折进 `other` |
| `progress.py` | 阶段标头、实时百分比、心跳、失败报告 |
| `align-model-config.py` | 修复 `chunk_size` 会导致分块器崩溃的模型配置 |
| `setup-env.ps1` | 创建 `.venv`，安装 PyTorch 和两个推理后端 |
| `common.ps1` | 工作区解析、`歌手 - 曲名` 命名工具 |

## 后续计划

- 自动生成 Cantabile 的 rack/song 配置（Performer triggers / Network Server）
- ELB-02（右手/左手/脚）→ Cantabile 的实时 MIDI 路由
- 分离失效段落的兜底方案（目前只报告，不修复）
- 等出现可免费使用的钢琴 / keys 专用模型后，再细分钢琴

## 相关文档

- [prerequisites_zh.md](prerequisites_zh.md) —— 把 ELB-02 当作 MIDI 控制器（中文）
- [prerequisites.md](prerequisites.md) —— English
