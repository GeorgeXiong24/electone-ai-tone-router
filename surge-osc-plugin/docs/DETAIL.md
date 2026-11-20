# Surge XT OSC tools

通过 OSC 直接**设置**与**查询** Surge XT 合成器参数的工具包，是
「Surge XT 音色复刻混合工作流」的执行层：AI 决策层（DeepSeek + surgext-mcp
参数文档）决定参数值，本插件负责把值可靠写入 Surge XT 并读回验证。
本插件不渲染、不分析音频，只做通用 OSC 通道，零运行时依赖。

## 工具

### `surge_set_param`

把一个 Surge XT 参数设置为归一化值（0–1）。

| 参数 | 说明 |
| --- | --- |
| `address` | Surge XT OSC 参数地址，如 `/param/filter_1/cutoff`，必须以 `/param/` 开头 |
| `value` | 归一化值 0–1（Surge OSC 层只接受归一化值，Hz/ms 等物理量由 AI 决策层换算） |
| `host` / `port` | Surge XT 所在主机与 OSC 输入端口，默认 `127.0.0.1:9000` |

### `surge_get_param`

查询一个参数的当前归一化值，同时返回 Surge 自己的显示字符串（如 `523.25 Hz`）。

| 参数 | 说明 |
| --- | --- |
| `address` | 同上 |
| `host` / `port` | 同上 |
| `outputPort` | 本机监听回复的 UDP 端口，必须与 Surge XT 的 OSC **输出**端口一致（默认 9001） |
| `timeoutMs` | 回复等待上限，默认 500ms |

## Surge XT 侧准备

1. Surge XT → Settings → OSC：启用 OSC，设置输入端口（默认 9000）。
2. **查询（surge_get_param）** 还需启用 OSC 输出：输出 IP 指向运行 DSH 的
   电脑，输出端口设为工具的 `outputPort`（默认 9001）。Surge 把查询回复发往
   它自己配置的 OSC 输出端口/地址，而不是回发给请求方——这是查询超时的最常见
   原因，超时错误信息里会给出排查提示。
3. 参数地址以 Surge XT 内置的 OSC 规范（菜单中的 OSC specification）为准。

## 已知限制

- 只处理参数设置/查询；音符（`/mnote`、`/fnote`）与 patch 加载不在第一版范围内。
- 查询必须配置 Surge 的 OSC 输出，否则 `surge_get_param` 只会超时（错误信息可操作）。
- 参数值一律为归一化 0–1，本插件不做任何单位换算。
