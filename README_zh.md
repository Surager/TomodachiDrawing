# TomodachiDrawing

《朋友聚会新生活》（*Tomodachi Life*）绘图宏生成工具集。

## 摘要

本仓库实现将栅格图像转换为与游戏内绘图界面兼容的、带时间戳的手柄输入序列。流水线在给定调色板上执行颜色量化，可选地依据画笔足迹进行路径规划，并通过蓝牙模拟的 Nintendo Switch 控制器进行回放。**除具备蓝牙射频模块的通用计算机外，本项目不依赖任何专用外设。** 主机侧执行通常依托 Linux 用户态栈暴露虚拟 Pro Controller（例如通过 `nxbt`）；在 Windows 或 macOS 上，常见做法是在 Linux 虚拟机中运行上述软件，并借助 USB/IP（`usbip`）等机制将宿主机的蓝牙适配器转发至客户机，使其在虚拟机内呈现为 USB 设备，从而满足 Switch 侧配对与握手的预期。

## 仓库结构

- `main.py`：默认入口；`--mode pixel` 为逐像素（1×1）绘制，`--mode brush` 为基于覆盖的画笔绘制
- `main_fast.py`：较快版本的像素生成器，含基础路径规划
- `main_brush.py`：考虑六种画笔尺寸的生成器
- `ctrl.py`：`nxbt` 宏执行脚本
- `preview.py`：量化预览与像素点导出
- `sequence_preview.py`：将已生成按键宏回放到预览 PNG
- `webui.py`：基于 Flask 的 Web 界面，支持手柄连接、按键测试、图像上传、预览与绘制进度

## 模型假设

- 画布分辨率为 `256×256`。
- 输入图像按 `RGBA` 解释；完全透明的采样予以忽略。
- 颜色量化至游戏绘图面板网格：
  - 色相（Hue）：200 档
  - 饱和度（Saturation）：214 档
  - 亮度（Brightness）：112 档
- 画笔足迹为正方形，边长为奇数：`1`、`3`、`5`、`13`、`19`、`27`。
- 画笔选择与颜色选择的状态在界面多次打开之间保持（与生成器建模一致）。
- 宏文本保留 `BUTTON 0.075s` 形式，含 `0.075s` 驻留间隔。

## 典型工作流

```bash
python main.py --mode pixel picture.png > macro.txt
python main.py --mode brush picture.png > macro.txt
python main.py --mode brush --merge-threshold 8 picture.png > macro.txt
python ctrl.py macro.txt
python preview.py picture.png -o preview.png --dump points.json
python sequence_preview.py macro.txt -o sequence_preview.png
python webui.py
```

启动 `webui.py` 后，在浏览器中访问 `http://127.0.0.1:50000`。

若手柄连接失败，请保留终端会话并查看打印的 traceback。建议先以普通模式运行；`--debug` 会关闭 Flask 的重载器，以避免在 `nxbt` 周围产生额外进程。

## 绘制前的初始准备

在向游戏内画布回放宏之前，请将编辑器状态与生成器的假定对齐：

1. 将画笔调整为**最小的正方形**（最小足迹）。
2. 将当前**自定义颜色**选定在自定义颜色区域的**左下角**格位。
3. 将画笔光标置于绘图**画布左上角**。

## 依赖管理（`uv`）

```bash
uv sync
uv run tomodachi-webui
```

亦可通过 `uv run` 调用等价的脚本入口，例如：

```bash
uv run tomodachi-generate --mode brush picture.png
uv run tomodachi-generate --mode brush --merge-threshold 8 picture.png
uv run tomodachi-preview picture.png -o preview.png
uv run tomodachi-sequence-preview macro.txt -o sequence_preview.png
uv run tomodachi-control macro.txt
```

## 附注

- `main.py` 默认使用 pixel 模式。
- `ctrl.py` 依赖 `nxbt` 与 `tqdm`。
- 若以吞吐量为优先、对严格逐像素保真度要求较低，建议使用 `main_brush.py` 作为生成入口。

## 局限性

宏的回放经由蓝牙链路与主机端、游戏机侧控制器模拟完成。实际部署中，时延波动、HID 报告偶发丢失或乱序、以及短时断连等现象较为常见。**因此，端到端的绘制结果无法保证以概率 1 与合成宏或生成器理想化时序完全一致**——不应假定意图与主机画面在像素或笔触层面能够百分之百吻合。

## 许可

本项目采用 **[PolyForm 非商业许可证 1.0.0](LICENSE)** 发布，**仅限非商业使用**；完整条文见仓库根目录 `LICENSE`，并在 `pyproject.toml` 中以 SPDX 标识符 `PolyForm-Noncommercial-1.0.0` 声明。

**明知是非商用条款还硬拿去变现赚钱的——滚你妈的，是不是给脸不要脸。**  
**别装没看见许可证：你心里清楚自己在干什么脏活。**

## 来源说明

本项目约 **99.9%** 由 AI 生成。
