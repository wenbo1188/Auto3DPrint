# Auto3DPrint

让 Bambu Lab P2S 白天排队、夜里无人值守连续打印的自制系统。

整体路线：打完 → 热床冷却 → 工具头把零件推下床、靠重力滑进收料箱 → 摄像头确认床面已空 → 开始下一盘。

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1 | Windows 控制程序：连接打印机、看状态、上传并开始打印 | 进行中 |
| 2 | 清板实验：清板 G-code、刮板头罩、后部垫高脚垫 | 未开始 |
| 3 | 排队调度 + 摄像头验空，跑通夜间自动循环 | 未开始 |
| 4 | 安全兜底：智能插座远程断电、烟雾报警 | 可选 |

## 第 1 阶段：控制程序

### 打印机准备

1. 打印机屏幕：**设置 → 网络**，打开**局域网模式（LAN Only）**，再打开**开发者模式**。
2. 记下屏幕上的 **IP 地址** 和 **访问码（Access Code）**。
3. 记下**序列号**：在 **设置 → 设备** 里，或者机身背面的标签上。
4. 确保打印机里插了存储卡（文件会上传到那里）。

> 开了局域网模式后，Bambu Handy 手机 App 就用不了了，这是 Bambu 的限制。

### 运行

**方式一：直接用 exe（推荐）**
在仓库 Actions 页面找到最新一次 `build`，下载 `Auto3DPrint-windows` 里的 `Auto3DPrint.exe`，双击运行。

**方式二：用 Python 运行**
```bat
pip install -r requirements.txt
python run_gui.py
```

**自己打包 exe：** 双击 `build_exe.bat`，生成 `dist\Auto3DPrint.exe`。

### 使用

1. 填 IP、序列号、访问码，点**连接**。状态区开始刷新，就说明 MQTT 通了。
2. 点**测试文件传输**，日志里能列出存储卡上的文件，就说明 FTPS 通了。
3. 在 Bambu Studio 里切片后，用 **文件 → 导出 → 导出切片文件（Export plate sliced file）** 得到 `.3mf`。
4. 选择这个文件，点**上传并开始打印**。

如果文件上传成功但打印机没开始打印，把**启动地址前缀**换成 `file:///sdcard/` 再试一次，并把你用的那个记下来告诉我（P2S 用哪个还没实机确认）。

设置保存在 `%APPDATA%\Auto3DPrint\config.json`。

## 开发

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

测试用本地 MQTT broker 和 FTP 服务器模拟打印机，不需要真机。

代码结构：

- `autoprint/printer.py`：MQTT 连接、状态合并、打印 / 暂停 / 停止 / 发 G-code
- `autoprint/ftps.py`：隐式 FTPS 上传（990 端口，数据通道复用 TLS 会话）
- `autoprint/threemf.py`：检查 3mf 是否包含切片好的 G-code
- `autoprint/gui.py`：Windows 窗口（tkinter）
