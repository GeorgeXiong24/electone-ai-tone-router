# surge-osc-plugin — Surge XT OSC 参数工具（DSH 插件）

「Surge XT 音色复刻混合工作流」的**执行层**：让 AI Agent 通过 OSC 协议直接
设置与查询 Surge XT 合成器的参数（[prompt.md](../prompt.md) 方案的第三层）。

- 算法层（`scripts/analyze-tones.py` + `scripts/tone_design.py`）：提取目标
  音频的客观声学特征并设计参数值。
- AI 决策层（DeepSeek + surgext-mcp 参数文档）：推理应调整的参数与目标值。
- **执行层（本插件）**：把参数值可靠写入 Surge XT，并读回验证。

插件只负责「发送 OSC 消息设置/查询参数」，不渲染、不分析音频；零运行时依赖
（OSC 编解码为自带实现，UDP 用 Node.js 原生 `dgram`）。

## 工具

### `surge_set_param` — 设置参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `address` | string，必填 | Surge XT OSC 参数地址，如 `/param/filter_1/cutoff`，必须以 `/param/` 开头 |
| `value` | number，必填 | 归一化值 0–1。Surge 的 OSC 层只接受归一化值，**不要**把 Hz/ms 反归一化后发送；物理量 → 归一化值的换算由 AI 决策层完成 |
| `host` | string，可选 | Surge XT 所在主机，默认 `127.0.0.1` |
| `port` | number，可选 | Surge XT OSC 输入端口，默认 `9000` |

发送一条 UDP OSC 消息后返回确认（地址 + 发送的值 + 目标端点）。

### `surge_get_param` — 查询参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `address` | string，必填 | 同上 |
| `host` / `port` | 可选 | 同上 |
| `outputPort` | number，可选 | 本机监听回复的 UDP 端口，默认 `9001` |
| `timeoutMs` | number，可选 | 回复等待上限，默认 500ms |

发送 `/q` + 地址的查询消息并等待 Surge XT 的回复（归一化值 + Surge 自己的
显示字符串，如 `523.25 Hz`）。超时或 Surge 报错时返回 `ok:false` 与可操作的
错误信息。

**查询的重要限制：** Surge XT 把查询回复发送到它自己配置的 **OSC 输出
IP/端口**，而不是回发给请求方。因此查询功能要求：

1. Surge XT 设置里启用 OSC 输出，输出 IP 指向运行 DSH 的电脑；
2. Surge 的 OSC **输出端口**与工具的 `outputPort` 参数一致（默认 9001）。

两者不一致时 `surge_get_param` 只会超时——错误信息里包含这条排查提示。
参数写入（`surge_set_param`）不受此限制，只需 OSC 输入端口正确。

**参数地址**以 Surge XT 内置的 OSC 规范（菜单中的 OSC specification）为准，
例如 `/param/filter_1/cutoff`、`/param/osc_a_1/pitch`；本插件不硬编码任何
参数表，只校验 `/param/` 前缀并原样转发。

## 在 Surge XT 中启用 OSC

1. Surge XT → Settings → OSC → 启用 OSC，输入端口保持默认 9000（或与工具
   的 `port` 一致）。
2. 需要查询时：启用 OSC 输出，输出 IP = 运行 DSH 的电脑，输出端口 = 9001
   （或与工具的 `outputPort` 一致）。

## 安装到 DeepSeek Harness

插件是一个标准 DSH bundle（`package.json` 的 `dsh.bundle.patch` +
`cordis.patch.yml`），安装任选其一：

- DSH Web 客户端：**Plugins → Add plugin**，选择本目录；
- 或命令：`plugin_manager` `install_bundle`，target 为本目录的绝对路径。

安装后在插件列表出现「Surge XT OSC tools / Surge XT OSC 参数工具」，两个工具
即可在对话中被 Agent 调用。

## 开发

```
surge-osc-plugin/
  src/index.ts        插件入口：注册 surge_set_param / surge_get_param
  src/osc-client.ts   OSC 编解码 + UDP 发送/查询（含超时与错误诊断）
  src/dsh-shims.d.ts  DSH 运行时包的编译期类型替身
  lib/                与 src 等价的构建产物（运行时加载入口）
  test/selftest.mjs   离线自检（不依赖 DSH / Surge）
  package.json        bundle 清单（dsh.bundle.patch）
  cordis.patch.yml    Loader 插件行
  tsconfig.json       tsc 配置（可由 src 重新生成 lib/）
  locale/  docs/  icon.svg   插件管理器展示元数据
```

- **零依赖**：OSC 1.0 编解码为自带实现（源码内注明协议细节），UDP 用
  `node:dgram`；安装本 bundle 不需要 npm install，也不会产生任何缓存。
- `lib/` 是 `src/` 的手写等价构建产物（本机无 tsc 时的交付形态）；装有
  TypeScript 时可在本目录运行 `tsc` 重新生成。
- 自检：`node test/selftest.mjs`（编解码往返、UDP 回环查询、超时诊断）。

## 已知限制

- 第一版只做参数设置/查询；`/mnote`、`/fnote` 音符与 `/patch/load` 不在范围内。
- 查询必须配置 Surge 的 OSC 输出（见上），否则超时。
- 不做任何单位换算：入参/出参都是归一化 0–1（查询额外返回 Surge 的显示字符串）。
