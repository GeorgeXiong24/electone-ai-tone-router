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
| 生成 Cantabile（ELB-02）配置 | 已完成 |
| 自动导入 Cantabile | 已完成 |

## 实现原理

1. **下载** —— `scripts/download-audio.ps1` 用曲名搜索 YouTube（或直接用 URL），
   把 44.1kHz / 16-bit 立体声 wav 存到工作区。视频标题会被压缩成简短的
   `歌手 - 曲名`。
2. **分离** —— `scripts/separate-stems.ps1` 在本地 GPU 上跑 BS-RoFormer-SW，
   把混音分成 `vocals`、`drums`、`bass`、`guitar`、`piano`、`other`（外加 `instrumental`）。
   输出 32-bit float wav。
3. **检测** —— `scripts/check-stems.py` 逐窗口扫描**模型的原始输出**并报告失效段落
   （见[分离的局限](#分离的局限)）。
4. **细分**（可选）—— `scripts/refine-stems.py` 只对真正有声音的轨运行专用模型，
   在父轨旁生成更细的音色。目前支持 drums → `kick` / `snare` / `toms` / `hh` / `ride` / `crash`。
5. **分析** —— 判定哪个乐器是主旋律、哪些是和弦/副旋律、哪个是低音线条。
6. **映射与配置** —— 为每个声部挑选对应的 VST 音色，并生成 Cantabile 工程：
   右手键盘 = 主旋律（MIDI 通道 1），左手键盘 = 和弦/副旋律（通道 2），脚键盘 = 低音（通道 3）。
7. **导入** —— 生成 `.cantabileSong` 后询问是否自动用 Cantabile 打开，插件与音色一步到位。
8. **演奏** —— ELB-02 只当 MIDI 控制器，电脑发声并通过 AUX IN 回到琴的音箱。
   硬件接线见 [prerequisites_zh.md](prerequisites_zh.md) / [prerequisites.md](prerequisites.md)。

注意：分离出来的是**乐器轨**，不是音乐角色；"主旋律 vs 和弦"由分析步骤判定，而不是分离器。

每个阶段都会打印编号、实时百分比和已耗时，运行慢和卡死一眼可辨。
详见[进度显示](#进度显示)。

## 分离的局限

在拿分离结果做后续开发之前，请先读这一节。

- **`other` 不是乐器轨**，它是残差桶——按定义
  `other = 混音 − (vocals + drums + bass + guitar + piano)`。里面装着铜管、pad、弦乐
  以及其他模型无法归类的成分。**不要把 `other.wav` 当作"钢琴轨"或任何单一音色。**
- **但残差桶里装的是真实乐器，不是垃圾。** 它不能直接当单一音色用，却也不该直接丢弃：
  先测里面是什么，再决定配什么音色。参考曲目中的 `other` 可以主要包含**弓弦**，
  它具有较长的起音与持续段、很低的谱平坦度。这些特征钢琴不一定能满足。
  **用 pad 预设去还原弦乐会得到金属感的假音色**，必须换成弦乐预设（如 `Violini Poly`）。
  判断方法：起音时间 + 最长持续段 + 谱平坦度，三项一起看。
- **即使原曲有钢琴，`piano` 轨也可能是空的。** 模型准确率与曲目相关，
  某首曲目的钢琴内容可能落在 `other` 里，也可能在另一首曲目中正常分离。
  **准确率与曲目相关，依赖某个轨之前请先验证。**
- **稀疏前奏可能整体失效。** 某些曲目的开头段落可能出现 `corr(other, mix) ≈ 1.000`，
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
- 约 6GB 磁盘空间（`.venv` 约 4.9GB，加上两个必下模型约 1.1GB；用 53 轨模型再 +1.3GB）

## 安装

```powershell
.\scripts\setup-env.ps1
```

会创建 `.venv`、安装 CUDA 版 PyTorch 和 `bs-roformer-infer`。分离模型（约 667MB）
在首次运行时自动下载到 `<工作区>\models\`，详见[模型下载](#模型下载)。

同时会安装 `audio-separator`，它只用于可选的细分阶段。该包锁定了一些较旧的依赖版本，
直接安装会破坏 `bs-roformer`，所以脚本用 `--no-deps` 安装并显式补上需要的组件——
**不要"顺手修好"成普通安装**。

`tools/` 目录需要有 `yt-dlp.exe`、`ffmpeg.exe`、`ffprobe.exe`（二进制不入库，脚本从这里查找）。

### 模型下载

**仓库里不含任何模型权重**——`workspace/` 整个目录都在 `.gitignore` 中。克隆下来的是纯代码，
模型在你第一次真正跑任务时**自动下载**，不需要建目录、不需要填路径、不需要改任何配置。

| 模型 | 大小 | 何时下载 | 落到哪里 |
| --- | --- | --- | --- |
| BS-RoFormer-SW（6 轨主模型） | 667 MB | 首次运行 `separate-stems.ps1`（被 `download-audio.ps1` 自动接续调用）时 | `<工作区>\models\roformer-model-bs-roformer-sw-by-jarredou\` |
| MDX23C DrumSep（鼓组细分） | 417 MB | 首次运行 `refine-stems.py`（可选的二级细分）时 | `<工作区>\models\audio-separator\` |
| 53 轨 "mega" 模型 | 1.31 GB | **只有**显式 `-Model roformer-model-bs-roformer-mvsep-mega-53-stems` 才下载，默认永不触发 | 同上 |

目标目录不存在时会被自动创建。下载带 SHA-256 校验，中断的下载写在 `.partial` 临时文件里、
完成后才改名，所以不会留下半截文件被后续运行当成"已存在"。

**网络受限 / 离线时**：两个下载源都是直连 `huggingface.co` 和 `github.com`，没有内置镜像回退。
两条路可走：

1. 自备代理（`HTTP_PROXY` / `HTTPS_PROXY` 环境变量即可，两个库都走 `requests`）；
2. 手动放文件，路径必须精确：

```
<工作区>\models\roformer-model-bs-roformer-sw-by-jarredou\
    BS-Rofo-SW-Fixed.ckpt      ← 699,412,152 字节
    BS-Rofo-SW-Fixed.yaml

<工作区>\models\audio-separator\
    MDX23C-DrumSep-aufr33-jarredou.ckpt
    config_drumsep_mdx23c.yaml
```

也可以把模型放在任意位置，用环境变量指过去：`BS_ROFORMER_MODELS_PATH`（bs-roformer）、
`AUDIO_SEPARATOR_MODEL_DIR`（audio-separator——注意这个目录必须**事先存在**，
否则 `Separator()` 初始化会直接抛 `FileNotFoundError`）。

**别塞来源不明的同名文件**：`audio-separator` 对已存在的模型文件也会校验 SHA-256，
不匹配会删掉重下；`bs-roformer` 只在下完后校验，不检查已有文件。
两个源都没有记录校验和时，只会做文件大小检查。

## 使用方法

### 一键运行

双击 `download-audio.cmd`，依次输入曲名（或 URL）和工作区目录，下载和分离会自动接续执行。

### 下载

```powershell
.\scripts\download-audio.ps1 "歌手 - 曲名"
.\scripts\download-audio.ps1 "https://www.youtube.com/watch?v=..."
.\scripts\download-audio.ps1 "歌手 - 曲名" -CookiesFromBrowser chrome
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
.\.venv\Scripts\python.exe .\scripts\refine-stems.py --stems "workspace\stems\<歌曲名>"
.\.venv\Scripts\python.exe .\scripts\refine-stems.py --latest --dry-run
```

只对真正有声音的轨运行专用模型，结果写到 `<歌曲名>\refined\`。
某个专用模型下载失败会被跳过并提示，**不会丢弃**其他模型已产出的结果。

这取代了早先"按响度裁到 N 轨"的做法——那种做法只是改了个响度阈值，无法产生新音色。

### 质量检测

```powershell
.\.venv\Scripts\python.exe .\scripts\check-stems.py --stems "workspace\stems\<歌曲名>" --mix "workspace\audio\<歌曲名>.wav"
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
.\.venv\Scripts\python.exe .\scripts\analyze-tones.py --song <歌曲名>
.\.venv\Scripts\python.exe .\scripts\build-tones.py --song <歌曲名>
```

- `analyze-tones.py` 逐轨识别乐器、音色特点（亮度、音区、谐波、同时音数）并按 5 秒窗口
  判定音乐角色（主旋律 / 副旋律 / 和弦 / 伴奏 / 和声铺底 / 低音线条 / 节奏）。
  输出 `workspace\analysis\<歌曲>\analysis.json` 和可读的 `analysis.md`。
  同时测量驱动下面音色参数设计的声学特征：起音对齐的典型音符包络
  （起音 / 衰减 / 持续 / 释放 + 打击性 / 衰减型 / 持续型倾向）、
  起音与持续段的亮度对比（谱质心）以及 5 维 MFCC 均值。
- **钢琴轨里藏着两条线。** pyin 跟的是最响的基频，在钢琴上几乎总是低音伴奏音，
  所以直接分析会得出"钢琴 77% 在 C2–C3、没有旋律"的错误结论。实际钢琴轨
  C5 以上占 50–66%。`analyze_melody_register()` 先高通到 C4 以上再单独跟踪，
  用「相邻帧 <2 半音的比例 >85%」区分旋律与和弦跳变。
  于是**钢琴可以同时占右手（旋律）和左手（和弦）**，这正是实际弹法。
  高通后必须检查高频段能量占比，否则贝斯的泛音会被误判成旋律。
  这个门槛要**远高于 5%**：40 Hz 贝斯的第 7、8 次谐波就足以越过 5%。实测分离度：
  贝斯 0.052（纯泛音）／吉他 0.577–0.695／钢琴 0.703–0.748／残差桶 0.834–0.927，
  门槛取 **0.15** 落在空档里。贝斯泛音过强时可能把低音冒充成主旋律，
  遮盖真正的旋律 stem。
  **不要用"音区差"做判据**：实测吉他真实高音线比自身和弦音区高 2.1 个八度，
  任何能拦住贝斯的音区阈值都会同时杀掉这条真旋律。
- `--exclude vocals,other` 可排除指定轨。排除人声后右手主旋律由钢琴高音区承担。
- `build-tones.py` 把分析结果生成**单乐器**音色文件，写到
  `workspace\tone\<曲目>\<右手|左手|脚>\<插件>\`——一个文件绝不混合多个乐器。
  右手 = 主旋律，左手 = 副旋律/和弦/伴奏，脚 = 低音线条；鼓组不生成音色文件
  （节奏由 ELB-02 自带 rhythm 处理）。
  同一 stem 分到两个键盘时，文件名按角色区分（如「钢琴(主旋律)」与「钢琴(和弦)」）。
- **曲目目录用拼音 ASCII 名**（如 `xiaomi-wozouhou-695243fd`），不是中文曲名。
  原因和键盘目录用 `right`/`left`/`pedal` 是同一个：ARIA 用系统代码页解码整条
  `.sfz` 路径，路径里任何一段中文都会变乱码、音色静默加载失败。目录名由
  `scripts/tone_paths.py` 从曲名确定性生成（拼音 + 曲名哈希前 8 位，防止不同曲名
  拼音相同而撞车），所以重建会落回同一目录；可读曲名写在目录内的 `song.txt`
  和 manifest 的键里。
- **Surge 音色是按音频设计的，不是只换标签。** `scripts/tone_design.py` 把分析出的声学特征
  映射为 Surge XT 补丁参数——滤波截止频率取自谱质心、滤波包络深度取自起音/持续亮度对比、
  放大与滤波包络取自实测 ADSR、共振取自谐波占比——`build-tones.py` 把这些值写进 `.fxp`
  （模板预设的振荡器设计保留）。每个设计值及其依据都会记录到 `tone\manifest.json` 和
  补丁注释里；加 `--no-param-design` 可退回纯模板复制。
  设计**受模板约束**，因为测量值不等于音色设计：
  - 截止频率最多只能在模板自身 cutoff 的 ±3 八度内移动，否则一个偏暗的轨道会把明亮的
    音色拖成闷掉的正弦波；
  - **但模板约束必须先于基频下限生效。** 这两条规则的先后顺序曾经把每一件贝斯音色
    都变成静音：模板 cutoff 在 −51 半音（13.75 Hz），先按 ±3 八度夹紧得到 −15 半音
    = 185 Hz，再想套「不低于 1.5×基频」已经晚了——185 Hz 对 74 Hz 的基频仍是致命低通。
    实测该音色峰值为 0.0004（模板原值 0.114），也就是**整个脚键盘没有声音**。
    现在模板夹紧先做，硬下限后做，硬规则赢。
  - 基频下限是 **基频以上 3 个八度（8 倍）**，不是 1.5 倍。实测同一模板在 74 Hz 基频下：
    111 Hz → 峰值 0.0000、222 Hz → 0.0004、444 Hz → 0.0043（都听不见），
    592 Hz → 0.0323 才开始有声。24 dB/oct 低通需要好几个八度的余量，
    承载贝斯响度的谐波才能活下来。
  - 非低通滤波器完全保留模板 cutoff——陷波滤波器没有「亮度」可以打开；
  - 模板的滤波包络深度只要达到设计值的一半就保留，从而保住击弦类乐器的起音特征；
  - 实测起音有 8ms 下限（起音检测在上升之后才触发，会低估），实测衰减短于倾向区间下限时
    以实测为准。
- 钢琴走采样音源（sforzando + Accurate-Salamander 大钢琴 SFZ）：SFZ 配置放在项目里，
  以绝对路径引用采样库。
- Surge 音色默认生成为 `.fxp` —— Surge 原生补丁格式，**拖进 Surge XT 窗口即可加载**
  （Surge 的拖放白名单包含 `.fxp`）。
  需要给 VST3 宿主（Cantabile）用时加 `--vstpreset`，会额外生成 `.vstpreset`；
  它只能由宿主自己的预设机制加载，不是 Surge 的拖放格式。
- **要"像原曲"，用采样音色**：Surge XT 是合成器、没有采样播放，工厂预设无法复现
  吉他/贝斯/弦乐的真实音色。`build-sample-tones.py` 从 stem 生成 sforzando 的 `.sfz` +
  对应 `.wav` 样本（每个 `.sfz` 及其 `samples/` 目录只含一件乐器），流水线全离线：
  起音 + 音高断裂切分、逐音质量筛选（voiced 比例、f0 稳定度、谐波性、谱平坦度）、
  2-3 个响度聚类力度层、**每个琴键各自按精确音高预渲染**并保留原频谱包络
  （引擎不做任何拉伸）、周期对齐的循环点且交叉淡化直接烘进 wav。
  人声不做采样（旋律由现场弹奏），钢琴仍用 Salamander 大钢琴采样库：

  ```powershell
  .\.venv\Scripts\python.exe .\scripts\build-sample-tones.py --latest
  ```

  输出到 `workspace\tone\<曲目>\<右手|左手|脚>\sforzando\`；`tone\sample-instruments.json`
  记录采纳/拒绝的音符数、力度层与键盘覆盖率。每个音色的 `coverage` 字段写明
  实际渲染键数、靠引擎移调补的键数、**留空的键**及覆盖百分比——
  没有可用素材的键如实留空，不硬拉伸。
- **绝不写入任何插件的默认/用户音色目录**——脚本会拒绝写到
  `workspace\tone\` 之外。`tone\manifest.json` 记录每次生成的文件清单和加载方式。

### 评估闭环（音色到底像不像，用数字回答）

```powershell
.\.venv\Scripts\python.exe .\scripts\tone_eval.py --song <歌曲名>
.\.venv\Scripts\python.exe .\scripts\tone_eval.py --song <歌曲名> --report
```

这条链路之前只会**写**音色，从不验证结果。`tone_eval.py` 补上后半程，
把每个音色渲染出来测三件事，结果写到 `workspace\tone\eval\<曲目>.json`：

| 检查 | 测什么 | 判据 |
| --- | --- | --- |
| **null test** | 引擎渲染的音频 vs 出厂 `.wav`，逐样本比对 | 残差 ≤ −60 dB 且相关 ≥ 0.99 |
| **音高审计** | 渲染音符的实测 f0 vs 按下的键 | 偏差 ≤ 50 音分 |
| **音色距离** | 渲染的谱质心 vs 源 stem 的谱质心 | 比值在 0.625–1.6 倍内 |

三条检查都刻意区分「乐器错了」和「测不准」：

- null test **会停在 `loop_start` 之前**。越过循环点后引擎重播的是文件里另一段音频，
  对贝斯来说那就是另一个音高——厂商 wav 的参考值可能偏离基准多个音分，
  若把循环段算进去会读成 +323 音分。
- 音高审计要求**至少 0.10 s 窗口**（82 Hz 只需 4 个周期才够 pyin 分辨；
  实测 50 ms 窗口会把 +8 音分读成 −127 音分），并对滑音/vibrato 给出
  `unstable`、对纯八度歧义给出 `octave_ambiguous`——**这些都不算音色失败**。
- 钢琴用的是外部 Salamander 采样库，不是本项目渲染的，null test 对它如实返回
  `not applicable`，而不是判失败。

### 生成并导入 Cantabile 工程

```powershell
.\.venv\Scripts\python.exe .\scripts\build-cantabile.py --song <歌曲名>
.\.venv\Scripts\python.exe .\scripts\import-cantabile.py --song <歌曲名>
```

`build-cantabile.py` 读取 `tone\manifest.json`，为每个 Surge XT 音色生成一个插件实例，
按键盘分配 MIDI 通道，输出 `workspace\cantabile\<歌曲名>.cantabileSong`：

| 键盘 | MIDI 通道 | 音色来源 |
| --- | --- | --- |
| 右手（上键盘） | 1 | 主旋律 + 和声铺底（可多个实例同时进通道 1，叠加发声） |
| 左手（下键盘） | 2 | 和弦 / 副旋律 / 伴奏（可多个实例同时进通道 2） |
| 脚（脚键盘） | 3 | 低音线条 |

- **同一声部，采样音色永远优先于合成器预设。** Surge XT 是合成器：它的工厂预设是振荡器设计，
  用它去模仿录到的吉他或贝斯，得到的是**另一件乐器**——尼龙吉他预设听起来像拨弦古筝，
  指弹贝斯预设听起来像弱音小号。只有 stem 本身携带录音的真实音色，所以当
  `build-sample-tones.py` 为某个声部生成了 `.sfz`，导入的就是它，同一声部的 Surge 补丁被跳过。
  钢琴是反方向的例外：始终用 Salamander 采样大钢琴。
- **采样音色的路径必须是纯 ASCII。** sforzando 的 ARIA 引擎用系统代码页（中文 Windows 上是 GBK）
  解码 `.sfz` 路径，而不是 UTF-8，所以中文路径会变成乱码（`左手` → `宸︽墜`），插件静默加载失败。
  因此键盘目录是 `right` / `left` / `pedal`，采样音色用 ASCII slug 文件名；可读的中文名保留在
  SFZ 注释和 manifest 里。Surge `.fxp` 保留中文名，因为 Surge 自己读它。
- **右手只归一个 stem。** 多个 stem 都可能有像样的高音线，但 ELB-02 只有一个右手。
  `analyze-tones.py` 会仲裁：高音旋律证据最强的 stem 拿右手，其余降为左手副旋律。
  残差桶 `other` 是叠加层，只有在右手空着时才占右手。
- **左手只留一个伴奏乐器。** 两个不同乐器响应同一个键不是叠加，是冲突：pad 和钢琴和弦一起响
  会互相拍频，听感就是「按一个音是古钟、按两个音是 jazz」。当已有真正的伴奏 stem 占左手时，
  残差桶从左手移除；贝斯只走脚键盘（在左手再叠一遍只会把和弦糊掉）。
- **每个渲染出的采样都做音高验证。** `render_note` 按 `f_target / f0_src` 移调，所以源采样的
  f0 若被检测成五度或八度偏移，每个键都会固定错一个音程——文件格式全对，乐器却在弹错音。
  `refine_f0()` 必须**双向**搜索：pyin 既可能锁到泛音（f0 偏高，真值是 f0/ratio），
  也可能锁到次谐波（f0 **偏低**，真值是 f0*ratio）。只向下搜索的版本无法修正后一种。
  判据不是"最低候选"而是**谐波序列完整度**：
  真基频的 1–8 次谐波都在，次谐波则缺掉奇数次。
  `verify_pitch()` 测量每个渲染结果，偏差超过 50 音分就**换下一个候选 take 重试**，
  而不是直接放弃该键——复调片段里 pyin 可能只跟到和弦中的一个音，另一个单音 take
  往往能正常渲染。全部候选都失败才留空。
- **力度层之间必须逐键回填。** 某个键在中间力度层的素材可能被音高验证拒掉，
  于是 SFZ 在轻/强两层有 region、中间层没有——弹到那个力度就完全不出声，
  用力一点又有声，听感就是"缺音"。`build_sample_tones` 渲染后按最近层回填并做增益补偿，
  保证每个键在三个力度层都有采样。
- **一次性采样必须尾部淡出到静音。** 没有 loop 的采样会被引擎一直持有到松键，
  如果文件在 −7 dB 电平时直接截断，声音就是"过 1–2 秒硬掐断"。
  `finalize_wav()` 对 one-shot 施加 220 ms 指数淡出（loop 采样只留 8 ms 防爆音），
  `ampeg_release` 从 0.25 s 提高到 1.2 s，松键后仍有自然余韵。
- **SFZ 的力度分层用 `<group>` 而不是 `<master>`。** 一个 SFZ 只有一个 `<global>` 和一个
  `<master>`，重复的 `<master>` 属于非法结构，ARIA 会静默忽略其中的 `lovel/hivel`，
  结果三个力度层对所有力度同时发声。
- **每个键都必须发声，这条优先于音色纯度。** 早期版本把"引擎移调补键"限制在手册 2 个半音以内，
  超出窗口的键**干脆不生成 `<region>`**——SFZ 里那些键显示为灰色、按下去没有任何声音。
  对一件演奏乐器来说这是最不能接受的失败模式：
  拉伸过的采样仍然是正确音高的音，缺失的 region 什么都不是。现在 `always_cover_keyboard` 保证
  硬件音域内每个键都有 region：优先用 ±2 半音内的 take **精确预渲染**（引擎零拉伸），
  超出的键则从最近的 take 预渲染（`render_note` 会保住频谱包络，比引擎重采样好听），
  再超出的由 SFZ 引擎移调补。拉伸超过 `extrapolation_warn_semitones`（3 个半音）的键会在
  manifest 里标为 `extrapolated`，**是质量提示，不是可弹性缺口**。
  移调上限依然存在，但它现在只决定"用哪个 take、并在报告里怎么标注"，**不再是让琴键静音的理由**。
- **每个键盘按自己的物理音域采样。** ELB-02 三个键盘音域不同——上/下键盘 49 键（C2–C6，
  MIDI 36–84），脚键盘 20 键（C2–G3，MIDI 36–55）——所以 `build-sample-tones.py` 按键盘分别
  构建乐器。这对脚键盘最关键：贝斯 stem 通常只弹 A1–D#2，**低于**脚键盘
  自己的音域，所以脚键盘上半部分没有源素材。这些键由 SFZ 引擎移调最近的已渲染键
  （`pitch_keycenter`）补足，这样采样保持在自己听起来仍像贝斯的音高，而不是被拉高一个八度。
  三个键盘现在都覆盖各自的全部键（49/49/20），实测零死键。
- 工程文件始终写在 `workspace\cantabile\` 内，**不会**写进 Cantabile 自己的用户目录。

`import-cantabile.py` 生成工程后会询问：

```
需要自动导入 Cantabile 吗？ [Y/n]
```

- `Y` —— 用 `Cantabile.exe` 打开工程，插件和音色自动就位。
- `N` —— 只保留工程文件，自己手动打开。
- 非交互场景用 `--yes` / `--no`；`--dry-run` 只生成不启动。

Cantabile 的插件状态是 `base64(raw-deflate(aBMP(VST3 state)))`，
`scripts/cantabile_format.py` 负责在 `.fxp` 与这个容器之间转换。
`build-cantabile.py --self-test` 会用真实工程文件做逐字节往返校验，
格式一旦变化会立刻失败。

### 实时控制：Surge XT OSC 插件

`surge-osc-plugin/` 是一个 DeepSeek Harness 工具包——即 `prompt.md` 中
「Surge XT 音色复刻混合工作流」的执行层。它让 Agent 通过 Surge XT 原生 OSC
接口设置（`surge_set_param`）与读回（`surge_get_param`）参数，零运行时依赖。
在 DSH 的 **Plugins → Add plugin** 里选择该目录即可安装（或用
`plugin_manager install_bundle`）；Surge 侧的 OSC 配置方法与查询限制
（Surge 把查询回复发往它配置的 OSC 输出端口）见
[surge-osc-plugin/README.md](surge-osc-plugin/README.md)。

### 工作区

所有步骤共用一个工作区根目录，优先级：

1. `-Workspace` 参数
2. 环境变量 `ELECTONE_WORKSPACE`
3. 项目根目录的 `config.json`（交互式选择路径后回答 `y` 自动写入）
4. 默认 `<项目根目录>\workspace`

目录结构：`audio\`（下载）、`stems\`（分离结果）、`stems\<歌曲名>\refined\`（可选的二级细分）、
`models\`（模型权重）、`tone\<曲目>\<右手|左手|脚>\<插件>\`（音色文件，曲目目录是拼音 ASCII 名，
见[音色分析与生成](#音色分析与生成)）、`cantabile\`（Cantabile 工程）、
`cache\`（工具缓存——torch / HuggingFace / numba / pip / yt-dlp 都被重定向到这里，
不再写入用户目录）。`config.json` 记录本机路径，已在 `.gitignore` 中忽略。

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
| [Surge XT](https://surge-synthesizer.github.io/) | 免费合成器 | GPLv3 |
| sforzando / Decent Sampler | SFZ / DSP 采样播放器 | 免费软件 |
| [Cantabile](https://www.cantabilesoftware.com/) | VST 宿主（Lite 免费） | 商业软件 |

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
| `tone_design.py` | 特征驱动的 Surge XT 参数设计（单位换算 + 决策规则） |
| `build-tones.py` | 生成 Surge XT 单乐器音色（`.fxp`；`--vstpreset` 可选） |
| `build-sample-tones.py` | 从每条 stem 提取单音，生成 sforzando 采样音色（`.sfz` + `.wav`） |
| `tone_eval.py` | **评估闭环**：null test + 音高审计 + 音色距离，把音色调成可测的数字 |
| `test_tone_rules.py` | 回归测试：cutoff 下限、模板约束顺序、单位换算、采样率一致性 |
| `cutoff_calibrate.py` | 渲染多档 cutoff 反查实测谱质心（独立工具，未接入 build 流程） |
| `render-tone.py` | 手工渲染单个音色并对比源 stem |
| `tone_paths.py` | 音色目录布局：曲目拼音 slug / 键盘 / 插件，以及 manifest 相对路径换算 |
| `migrate-tone-layout.py` | 一次性迁移：把旧的 `tone\<键盘>\<插件>\` 搬到 `tone\<曲目>\<键盘>\<插件>\` |
| `build-cantabile.py` | 生成 Cantabile 工程（`.cantabileSong`），按键盘分配 MIDI 通道 |
| `import-cantabile.py` | 生成工程并询问是否自动用 Cantabile 打开 |
| `cantabile_format.py` | Cantabile 插件状态容器（aBMP / VST3 state）与 `.fxp` 互转 |
| `prune-stems.py` | 保留最响的 N 轨，其余折进 `other` |
| `progress.py` | 阶段标头、实时百分比、心跳、失败报告 |
| `align-model-config.py` | 修复 `chunk_size` 会导致分块器崩溃的模型配置 |
| `setup-env.ps1` | 创建 `.venv`，安装 PyTorch 和两个推理后端 |
| `common.ps1` | 工作区解析、`歌手 - 曲名` 命名工具、缓存重定向 |
| `_cache_env.py` | 直接运行 python 脚本时把工具缓存重定向到 `<工作区>\cache` |

## 后续计划

- 分离失效段落的兜底方案（目前只报告，不修复）
- 等出现可免费使用的钢琴 / keys 专用模型后，再细分钢琴
- 支持 Decent Sampler 音色（`.dspreset`）作为 SFZ 的替代
- 把 `cutoff_calibrate.py` 接进 `build-tones.py`：它已经能渲染多档 cutoff 反查实测
  谱质心，可以把 `tone_design.py` 里 `centroid_to_cutoff = 1/0.18` 这个经验系数
  换成实测值。目前是独立工具，未接入 build 流程。

## 相关文档

- [prerequisites_zh.md](prerequisites_zh.md) —— 把 ELB-02 当作 MIDI 控制器（中文）
- [prerequisites.md](prerequisites.md) —— English
