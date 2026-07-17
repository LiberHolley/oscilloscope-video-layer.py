# Oscilloscope Video Player / 示波器视频播放器

**Repository:** `oscilloscope-video-layer.py`

| | |
| --- | --- |
| **English** | Play video outlines on an oscilloscope in XY mode through your PC sound card (L→X, R→Y), with optional synced soundtrack on a second speaker. |
| **中文** | 用电脑声卡立体声把视频轮廓画到示波器 XY 模式（左声道→X，右声道→Y），可选另一路扬声器同步播放原声。 |

硬件链路：

- **左声道 (L)** → 示波器 **CH1**（X，左右）
- **右声道 (R)** → 示波器 **CH2**（Y，上下）
- 电脑窗口同步显示原视频；可选另一路扬声器播原曲

目标示波器以 **RIGOL DS1202Z-E** 为例（其它双通道示波器流程类似）。

```text
视频 → 预处理轮廓缓存 (.npz)
         ↓
播放时：窗口显示原片 + 声卡扫轮廓 → 耳机/第二音频口 → 示波器 XY
         （可选）原曲 → 主板扬声器
```

仓库只保留这一条链路：


| 文件                   | 作用         |
| -------------------- | ---------- |
| `oscilloscope_video_player.py` | 预处理 + 同步播放 |
| `requirements.txt`   | Python 依赖  |


---

## 1. 环境准备

```powershell
cd <项目目录>
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

准备一份视频放到 `videos/`，例如 `videos/demo.mp4`，或使用仓库示例 `videos/cai.mp4`。

---



## 2. 硬件接线（声卡 → 示波器）



### 2.1 推荐接法

用主板 **独立耳机口 / Realtek 第二输出**（常见名：`Realtek HD Audio 2nd output`）专门接示波器，主扬声器留给音乐。

立体声 3.5 mm 插头（TRS）：

```text
        tip (L)  ───────────────> 示波器 CH1 信号（X）
       ring (R)  ───────────────> 示波器 CH2 信号（Y）
     sleeve (GND) ──────────────> 示波器 CH1 / CH2 地（共地）
```

示意：

```text
PC 耳机/第二音频口 (3.5mm)
        │
        ├─ L (tip)  ─── 探头/杜邦 ───> CH1
        ├─ R (ring) ─── 探头/杜邦 ───> CH2
        └─ GND      ─── 探头地夹 ───> 示波器地
```



### 2.2 接线注意

- **必须共地**：CH1、CH2、声卡地连在一起，否则画面会乱跳或漂移。
- 探头倍率拨 **1X**；也可用 3.5 mm 转双 RCA/BNC，或拆开旧耳机线直接焊 tip/ring/sleeve。
- 不要把 XY 信号和音乐打到**同一个**输出设备上，否则示波器噪声会进歌里。
- 线尽量短；避免和强干扰电源线捆在一起。
- 声卡输出一般是交流耦合、峰峰值约几百 mV～1 V 量级，示波器纵轴从约 **50～200 mV/div** 试起。



### 2.3 确认音频设备编号

```powershell
python oscilloscope_video_player.py --list-audio
```

示例（以你机器为准）：

```text
  [1] 扬声器 (Realtek(R) Audio)          ← 播音乐
  [2] Realtek HD Audio 2nd output ...  ← 接示波器（XY）
```

记下：

- `--speaker` = 接示波器的那一路（上例为 `2`）
- `--music-speaker` = 播音乐的那一路（上例为 `1`）

---



## 3. 示波器怎么调（RIGOL DS1202Z-E）

按顺序做；先接线再调菜单。

### 3.1 打开双通道

1. 按 **CH1**，确认通道已开启。
2. 按 **CH2**，同样开启。
3. XY 模式必须两路都开。



### 3.2 耦合与探头（CH1、CH2 都设）

对每个通道：

1. 按 **CH1** / **CH2** 打开该通道菜单。
2. **Coupling（耦合）** → 优先 **AC**（声卡输出常带直流偏置，AC 更稳居中）。若画面整体偏一边，再试 **DC**。
3. **Probe（探头）** → **1X**。
4. **Bandwidth Limit** 可关；若毛刺多可开 20 MHz。



### 3.3 进入 XY 模式

1. 按 **MENU** → **Horizontal**（水平），或直接找 **Time Base / 时基** 菜单。
2. **Time Base** → **XY**。
3. 确认 **X-Y** 映射为 **CH1 – CH2**（X=CH1，Y=CH2）。
4. 此时屏幕不再是普通 Y-T 波形，而是二维光点轨迹。



### 3.4 幅度与位置

1. 用 CH1 / CH2 的 **垂直档位旋钮** 调到图大致占屏 60%～80%（常见从 **100 mV/div** 起调）。
2. 用 **垂直位移** 把图案移到屏幕中央。
3. 若只有一条斜线或横/竖线：多半是某一声道没接上、声卡静音、或只开了单通道。
4. 若是一团糊斑：先把 PC 音量开大一点，并确认已是 XY 而不是 YT。



### 3.5 亮度 / 余辉（提高可见度）

视频轮廓扫得很快，DS1202Z-E **没有 Z 消隐**，轮廓之间会有跳线。

建议：

- **Intensity（亮度）** 调到能看清轨迹、又不刺眼。  
- 若有 **Persist / 余辉**，开短余辉，剪影更连贯。  
- 跳线无法完全消除，这是双通道示波器的物理限制，不是软件坏了。



### 3.6 快速自检（不跑完整视频）

1. Windows 声音设置里，把「正在播放」设备临时切到第二输出，播任意立体声测试音。
2. 示波器 XY 下应能看到随音乐变化的 Lissajous 图案。
3. 确认 L→CH1、R→CH2 后再跑本程序。

---



## 4. 软件用法



### 4.1 一键：预处理 + 播放

首次会生成缓存 `<视频同目录>/<名字>_xy.npz` 和原曲 wav，之后会直接复用。

```powershell
python oscilloscope_video_player.py --video "videos/cai.mp4" --speaker 2 --music-speaker 1 --music-volume 0.3
```

播放窗口可拖拽缩放，画面按比例留黑边。焦点在窗口时按 **q** 退出。

### 4.2 只预处理（不播放）

```powershell
python oscilloscope_video_player.py --video "videos/cai.mp4" --preprocess-only
```

强制重建缓存：删掉对应的 `*_xy.npz` 后再跑，或指定新的 `--cache` 路径。

### 4.3 只播放已有缓存

```powershell
python oscilloscope_video_player.py --video "videos/cai.mp4" --cache "videos/cai_xy.npz" --speaker 2 --music-speaker 1 --play-only
```



### 4.4 常用参数


| 参数                | 默认       | 说明                        |
| ----------------- | -------- | ------------------------- |
| `--speaker`       | 必填（播放时）  | XY 信号输出设备编号               |
| `--music-speaker` | `1`      | 原曲输出设备                    |
| `--music-volume`  | `0.35`   | 音乐音量 0～1，过大易削波            |
| `--no-music`      | 关        | 不播原曲                      |
| `--sample-rate`   | `384000` | XY 采样率（预处理写入缓存；播放以缓存为准）   |
| `--redraw-hz`     | `2000`   | 单帧轮廓每秒重扫次数，越高越不易「抽」       |
| `--vertices`      | `128`    | 每帧总采样点数预算                 |
| `--max-contours`  | `8`      | 最多保留的外轮廓数                 |
| `--contour-mode`  | `external` | `external` 只绕外轮廓；`list` 含孔洞 |
| `--jump-dim`      | `0.02`   | 轮廓间长跳线占用的采样比例（越小交叉线越暗）   |
| `--threshold`     | `128`    | 二值化阈值；`0` = Otsu          |
| `--foreground`    | `auto`   | `auto` / `dark` / `light` |
| `--bg-filter`     | 开        | 只抠运动前景画轮廓（`--no-bg-filter` 关闭） |
| `--bg-learn`      | `0.08`   | 背景学习速率                      |
| `--bg-motion-thresh` | `10`  | 运动判定阈值（相对背景/上一帧）          |
| `--bg-warmup`     | `8`      | 前 N 帧先建背景                    |
| `--motion-hold`   | `10`     | 运动短暂停止时沿用上一帧抠图的帧数           |
| `--swap-lr`       | 关        | 交换左右声道（X/Y 反了时用）          |
| `--no-invert-y`   | 关        | 取消 Y 轴翻转（上下反了时试）          |
| `--speed`         | `1.0`    | 播放倍速                      |


改 `--sample-rate` / `--redraw-hz` / 轮廓参数 / 背景过滤参数后，需要 **删除旧 `.npz` 并重新预处理** 才会生效。

### 4.5 日志里设备该长什么样

正常时应类似：

```text
[play] music -> [1] 扬声器 ...
[play] music device #.. ... [Windows WASAPI]
[play] XY/scope -> [2] Realtek HD Audio 2nd output ...
[play] XY device #5 ... [MME]          ← 高采样率常走 MME，属正常
[play] frames=... redraw≈2000Hz request_rate=384000
```

若出现 `Invalid sample rate`，程序会尽量换可用 host API / 降采样率；仍失败时换 `--speaker`，或把系统里第二输出的独占/格式限制关掉后再试。

---



## 5. 原理简述

1. **预处理**：每帧灰度 → 阈值剪影 → **相对背景/上一帧抠出运动区域** → 外轮廓绕行采样 → 写入 `.npz`。
2. **播放**：按视频帧率切换当前点列；声卡回调里以约 `--redraw-hz` 的速度反复扫这帧轮廓（L=X，R=Y）。长跳线只分极少采样，交叉线会暗很多。
3. **音乐**：用内置 ffmpeg 抽轨成 wav，在另一设备按时间轴同步播放。
4. **为何要高采样率 / 高重扫**：声卡与示波器都偏交流耦合，扫得太慢轮廓会「抽」；DS1202Z-E 无 Z 消隐，跳线只能减轻、不能根除。

运动抠图：学习近静止背景，只保留剪影里正在动的部分画轮廓；短暂停住会沿用上一帧（`--motion-hold`）。

---



## 6. 故障排查


| 现象                            | 排查                                                                 |
| ----------------------------- | ------------------------------------------------------------------ |
| `Invalid sample rate [-9997]` | 第二输出用 WASAPI 常拒 384 kHz；更新本仓库后应自动选 MME。仍失败则降 `--sample-rate` 后重建缓存 |
| 示波器一条线                        | 查 L/R 是否都接到 CH1/CH2；试 `--swap-lr`；确认设备不是静音                         |
| 只有噪声/糊团                       | 确认已进 **XY**；加大 PC 对该输出的音量；查共地                                      |
| 音乐和示波器串音                      | `--music-speaker` 与 `--speaker` 必须是不同设备                            |
| 上下/左右反了                       | `--no-invert-y` 或 `--swap-lr`                                      |
| 剪影太碎 / 太糊                     | 重建缓存：减小 `--max-contours`，或调整 `--threshold` / `--vertices`          |
| 静止背景/边框混进画面                   | 默认运动抠图已开；**删旧缓存重建**。仍残留可降 `--bg-motion-thresh` 或提高 `--bg-learn` |
| 角色停住就消失                       | 增大 `--motion-hold`；或略提高 `--bg-motion-thresh` 减少碎片          |
| 角色站着不动被抹掉                     | 增大 `--motion-hold`，或用 `--no-bg-filter` 对比                     |
| 窗口按 q 没反应                     | 先点击播放窗口再按 q                                                        |


---



## 7. 许可与素材

本仓库代码采用 [MIT License](LICENSE) 开源。

演示用的视频与音频素材请自行准备，并遵守其各自版权要求；仓库内示例视频的版权归原作者所有，不在本许可证覆盖范围内。