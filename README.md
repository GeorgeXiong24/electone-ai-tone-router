# electone-ai-tone-router

## 音频下载（yt-dlp + ffmpeg）

`tools/` 内已放置 `yt-dlp.exe`、`ffmpeg.exe`、`ffprobe.exe`，二进制文件不入库。

双击 `download-audio.cmd`，依次输入曲名（或视频 URL）和工作区目录（直接回车用默认），wav 会保存到 `<工作区>\audio\`。

也可以从命令行调用：

```powershell
.\scripts\download-audio.ps1 "千本桜"
.\scripts\download-audio.ps1 "https://www.youtube.com/watch?v=..."
.\scripts\download-audio.ps1 "夜に駆ける" -CookiesFromBrowser chrome
.\scripts\download-audio.ps1 "曲名" -Workspace D:\electone
.\scripts\download-audio.ps1 "曲名" -OutDir D:\somewhere
```

工作区目录的优先级：

1. `-Workspace` 参数
2. 环境变量 `ELECTONE_WORKSPACE`
3. 项目根目录的 `config.json`（交互式选择路径后回答 `y` 会自动写入）
4. 默认 `<项目根目录>\workspace`

后续的分离、分析脚本会共用同一工作区，分别使用其中的 `audio\`、`stems\`、`analysis\` 子目录。`config.json` 记录的是本机路径，已在 `.gitignore` 中忽略。

- 输入曲名时自动用 YouTube 搜索取第一个结果；B 站等站点可直接贴 URL。
- YouTube 要求登录验证时，加 `-CookiesFromBrowser chrome`（或 `edge` / `firefox`）。
- 输出固定为 wav（PCM 16-bit、44.1kHz、立体声），可直接喂给 Demucs 等分离工具。
