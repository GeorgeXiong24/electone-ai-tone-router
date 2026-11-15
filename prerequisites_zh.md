# 前置条件：把雅马哈 ELB-02 当作 MIDI 控制器，由 Windows 电脑发声

本文说明如何把 **雅马哈 ELB-02（STAGEA Basic）** 当作键盘/控制器，由 Windows 电脑上的软件音源发声。

> 型号说明：本指南针对 ELB-02 编写。其他双排键型号（ELS-02 系列、ELC-02、ELB-01 等）的接口、音色和 MIDI 行为可能不同。

## 1. 适用范围与限制

- ELB-02 通过 **USB TO HOST** 发送 MIDI，不传输音频。
- 外部自定义音色**无法装进 ELB-02**。电脑是音源，琴只负责键盘输入，必要时兼作功放。
- 必须把琴内音色静音，否则会和电脑音色叠在一起。
- 琴上没有 HDMI、没有用户采样内存，也没有官方的音色加载功能。

## 2. 硬件

- **USB 2.0 A–B 线**，长度小于 3 米。琴不支持 USB 3.0 的 A–B 线。
- **3.5mm 立体声线**：电脑耳机口/音频接口输出 → 琴的 **AUX IN**（立体声小插孔）。
- 可选：**USB 延音踏板**（琴本身没有延音踏板输入）。
- 可选：**USB 音频接口**，延迟更低、音质更好。
- 可选：有交流声时加地环路隔离器。

## 3. 软件（Windows 10/11，64 位）

- **Cantabile Lite**：VST 宿主。
- **ASIO4ALL**：仅在用板载声卡时需要；有音频接口就用接口自带 ASIO 驱动。
- **VST3 乐器插件**，例如：
  - Sforzando（SFZ 播放器）
  - Decent Sampler（`.dspreset` / `.dslibrary` 播放器）
  - Surge XT（合成器）
- **采样音色库**：SFZ 或 Decent Sampler 格式（钢琴、弦乐、贝斯等）。
- 可选：**MIDI-OX** 用于诊断，**ReaEQ** 或 **TDR Nova** 用于 EQ。

64 位 VST3 插件默认安装到：

```text
C:\Program Files\Common Files\VST3
```

ELB-02 在 Windows/macOS 上使用系统标准 USB-MIDI 驱动；除非连接不稳定，否则不需要安装雅马哈驱动。

## 4. 配置 Cantabile Lite

1. `Tools → Options → Audio Engine`
   - Audio Driver：`ASIO`
   - Device：`ASIO4ALL v2`（或音频接口）
   - Sample Rate：`48000`
   - Buffer Size：`256`（稳定后可降到 128）
2. `Tools → Options → Audio Ports`
   - 确认 Main Speakers 映射到输出通道 1/2。
3. `Tools → Options → Plugin Options`
   - 添加 `C:\Program Files\Common Files\VST3`。
4. `Tools → Scan Plugin Folders` → 选 `Full`。
5. 插入乐器（`Insert → Plugin...`）并加载音色库。
6. 用 USB 线连接 ELB-02。
7. `Tools → Options → MIDI Ports`
   - 新建端口，命名为 `ELB-02`。
   - 分配 ELB-02 的物理 MIDI 输入。
   - `Enabled` 开；`Default Port` 关；`Enable Loopback` 关；`Route MIDI Clock` 关。
8. 每个乐器建一条 MIDI 路由：
   - 上键盘 → 通道 `1`
   - 下键盘 → 通道 `2`
   - 脚键盘 → 通道 `3`
9. **每个键盘使用独立的插件实例**，避免同一个音互相抢音。
10. 保存工程（`File → Save As`）。

## 5. 琴端设置

- 将所有音色音量调为0。
- 发送 MIDI 时不要同时进行 MDR 播放或录音。

## 6. 音频路径

- 在 Windows 里选择连接了琴 **AUX IN** 的那个耳机口或音频接口。
- 用 ASIO 时，Windows 的默认输出不影响 Cantabile；要在 ASIO 控制面板里选择设备。

## 7. 测试清单

- 上、下、脚键盘分别只触发自己的乐器。
- 两个键盘弹同一个音高时不会互相切断。
- 听不到琴内自带音色。
- 128–256 采样缓冲下无爆音、无掉线。
- 工程和 `CTRL` 注册都有备份。

## 8. 已知限制

- USB TO HOST 只传 MIDI：不传音频，也不传文件。
- 没有用户采样/音色内存；外部音色只存在于电脑。
- 10 音复音限制只作用于琴内音源。
- USB TO HOST 和五针 MIDI 口互斥。
- 插入 USB 无线 LAN 适配器时，MIDI 和 USB TO HOST 都会被禁用。
- MDR 播放/录音时不能发送 MIDI。

## 9. 常见问题

| 现象 | 检查 |
| --- | --- |
| 没有声音 | 音频驱动/设备、插件是否加载、路由、输出电平 |
| 听到琴内音色 | 静音注册没有生效 |
| 同一个音被切断 | 两个键盘进了同一个插件实例；改用独立实例 |
| 音色发闷 | 关闭插件效果、选近场麦克风、加轻微高频 EQ |
| 延迟高 | 用 ASIO、128–256 缓冲、避免蓝牙音频 |
| Windows 音乐进不了琴 | 输出设备、线材，或 Cantabile 的独占模式 |
