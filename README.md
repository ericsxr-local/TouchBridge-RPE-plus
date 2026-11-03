# TouchBridge-RPE Plus

把 MacBook 触控板当成多点触控制谱台：跟着音乐点击、长按、滑动和快速甩动，实时生成 Tap、Hold、Drag、Flick，并导出适用于 Phira / RPE / PhiEdit 工作流的 `.pez` 谱包。

> 当前版本由 **Eric_SXR** 整合、修复与维护。  
> 本项目基于原作者 [@MiGuo2787 的 TouchBridge-RPE](https://github.com/MiGuo2787/TouchBridge-RPE) 继续开发，并使用社区长期流传的 macOS `MultitouchSupport.framework` 私有接口方案。**本维护版不声明为原创项目或原创触控方案。**

---

## 发布前必读

### 兼容性不是百分之百保证

触控板采集依赖苹果未公开的私有框架。苹果可能在不同机型、不同 macOS 或未来系统中改变接口，因此不能承诺所有 Mac 都能运行。

| 项目 | 状态 |
|---|---|
| Intel Mac | 已在 `MacBookPro16,1` 实机测试 |
| 测试系统 | macOS 13.7.8 |
| Apple Silicon | arm64 编译通过，但尚未完成真实机型触控回归 |
| 最低部署目标 | macOS 12.0 |
| MacBook 内置触控板 | 推荐 |
| Apple Magic Trackpad | 理论可用，尚未完整覆盖所有型号 |
| 第三方触控板 / iPad 模拟触控板 | 不保证可用 |
| Windows / Linux | 不支持原生触控采集 |

遇到问题时请在 Issue 中提供：

```bash
sw_vers
uname -m
sysctl -n hw.model
```

同时附上网页状态灯、报错截图和复现步骤。

### 项目定位

- 这是社区维护工具，不是 Phigros、Phira、RPE 或 PhiEdit 的官方软件。
- 官方 Phigros 客户端不能直接随意导入自制谱；本工具面向 Phira / RPE / PhiEdit 等自制谱工作流。
- `.pez` 的实际兼容性取决于目标编辑器或播放器版本。
- 请只使用自己拥有合法权利的音乐、图片和素材。

---

## 功能概览

- 读取 Mac 原始多点触控位置，不依赖鼠标光标坐标。
- 支持最多五个触点槽位。
- 自动识别 Tap、Hold、Flick、Drag。
- 连续坐标映射到 RPE 横向位置。
- BPM、Offset、拍号、细分、吸附容差和去抖可调。
- 支持 Flick 速度、Drag 速度、死区等手势参数。
- 浏览器播放音乐并提供滚动预览。
- 录制时尝试隐藏并锁定鼠标光标。
- 导出包含谱面、音乐和曲绘的 `.pez`。
- Web 与 OSC 只监听 `127.0.0.1`，不向局域网公开。
- 原生采集器有心跳、触控帧、触点和槽位诊断。

### 触控动作与音符

| 触控动作 | 生成结果 | RPE 类型 |
|---|---|---:|
| 快速轻点，移动很小 | Tap | 1 |
| 按住且移动很小 | Hold | 2 |
| 快速短距离甩动 | Flick | 3 |
| 有效持续移动 | Drag | 4 |

分类结果受死区、持续时间、速度和设备采样率影响，请根据自己的触控板调参。

---

## 工作原理

```text
Mac 触控板
    │ 原始多点触控数据
    ▼
TouchBridge.app（Swift 原生采集器）
    │ OSC / UDP 127.0.0.1:9000
    ▼
Python 后端（手势分类、节拍量化、导出）
    │ Socket.IO / HTTP 127.0.0.1:8765
    ▼
浏览器页面（音乐、录制控制、预览、导出）
```

原生采集器、Python 后端和浏览器页面缺一不可。网页能打开不代表触控采集器一定正常，请同时观察状态灯。

---

## 安装前准备

### 必需环境

1. macOS 12 或更高版本。
2. Python 3.10 或更高版本。
3. Xcode Command Line Tools（只用于编译 Swift 采集器，不需要安装完整 Xcode）。
4. Safari、Chrome、Edge 等现代浏览器。

检查 Python：

```bash
python3 --version
```

如果没有 Python 3，可选择：

- 从 [python.org](https://www.python.org/downloads/macos/) 安装；或
- 已安装 Homebrew 时执行 `brew install python`。

安装 Xcode Command Line Tools：

```bash
xcode-select --install
```

系统提示已安装时无需重复操作。

---

## 完整安装方法

### 方法一：下载 GitHub ZIP（推荐给普通用户）

1. 在仓库页面点击绿色 **Code** 按钮。
2. 选择 **Download ZIP**。
3. 解压 ZIP。
4. 建议把项目文件夹移动到 Mac 本地的“下载”“文稿”或其他固定目录。
   - 不建议长期直接放在 U 盘、只读目录、云盘占位目录或路径频繁变化的位置。
5. 双击项目根目录的 **`TouchBridge RPE.app`**。
6. 第一次运行会自动：
   - 检查并编译原生 TouchBridge 采集器；
   - 创建 `backend/venv` Python 虚拟环境；
   - 安装依赖；
   - 启动 Web 与 OSC；
   - 打开原生采集器和浏览器页面。

首次安装依赖需要联网，后续运行一般不需要重复安装。

### 方法二：双击 `run.command`

如果启动 App 没反应，可以右键 `run.command`，选择“打开”。它与启动 App 使用同一套启动流程，但会显示终端窗口，适合查看报错。

### 方法三：终端手动启动

```bash
cd /你的路径/TouchBridge-RPE-plus

# 编译原生采集器
bash TouchBridge/build.sh

# 创建后端环境
cd backend
python3 -m venv venv
source venv/bin/activate
python3 -m pip install -r ../requirements.txt

# 启动后端
python3 app.py
```

随后单独打开：

```bash
open ../TouchBridge/TouchBridge.app
open http://127.0.0.1:8765/
```

---

## 首次打开被 macOS 拦截

### 提示“无法验证开发者”

先右键点击对应 App，选择“打开”，再在弹窗中确认。

也可以在项目根目录执行：

```bash
xattr -cr .
chmod +x run.command
chmod +x TouchBridge/build.sh
chmod +x "TouchBridge RPE.app/Contents/MacOS/TouchBridge-RPE-Launcher"
```

之后重新双击 `TouchBridge RPE.app`。

### 输入监控权限

如果系统询问输入监控权限，请允许 TouchBridge。macOS 13 通常位于：

```text
系统设置 → 隐私与安全性 → 输入监控
```

授权后可能需要退出并重新打开采集器。若列表里没有 TouchBridge，可先运行一次，让系统生成权限记录。

> 由于采集器是本机临时签名，重新编译后 macOS 偶尔可能要求重新确认权限。

---

## 每次使用时怎么启动

1. 双击项目根目录的 `TouchBridge RPE.app`。
2. 等待浏览器自动打开 `http://127.0.0.1:8765/`。
3. 确认原生 TouchBridge 窗口正在运行。
4. 检查网页状态灯：
   - **服务连接**：浏览器已连接后端；
   - **OSC 监听**：UDP 9000 正在监听；
   - **触控板数据**：原生采集器心跳正常；
   - **音频已加载**：音乐已导入。

前三个与采集相关的状态必须正常，才能开始录制。

### 正确关闭

- 点击原生 TouchBridge 窗口左上角红色按钮会真正退出采集器。
- 程序不会在用户主动关闭后自动重新打开。
- 下次使用时重新双击 `TouchBridge RPE.app` 即可。
- 关闭采集器后，浏览器页面可能仍能打开，但不会再收到触控输入。

---

## 完整使用方法

### 1. 导入音乐

在网页“音频”区域选择音乐文件。导入完成后先点击播放，确认：

- 浏览器有声音；
- 进度正常移动；
- 音频时长正确；
- 页面没有格式错误提示。

建议优先使用常见 MP3、WAV、M4A 等浏览器支持的格式。不同浏览器对音频编码的支持可能不同。

### 2. 导入曲绘

曲绘可选。支持 PNG、JPG、GIF、WebP、BMP，文件上限 10 MB。

未导入曲绘时，导出会生成渐变占位图。

### 3. 填写谱面信息

建议在录制前填写：

- 谱面名称；
- 难度；
- 作曲者；
- 谱面设计（Charter）。

这些信息会写入 `.pez`。

### 4. 设置节拍

- **BPM**：歌曲速度。
- **Offset**：音乐起始偏移。
- **每小节拍数**：常见为 4。
- **细分**：常用 8 或 16。
- **吸附容差**：越高越容易吸附到节拍网格。
- **去抖时间**：防止一次点击被记成多个音符。

建议先用默认值录一小段测试，再逐项调整。

### 5. 调整触控参数

- **死区**：移动小于该距离时优先判为 Tap / Hold。
- **Hold 最短时间**：按住多久开始视为长按。
- **Flick 窗口和速度**：决定快速甩动是否识别为红键。
- **Drag 最小速度与最长时间**：避免长按时的轻微漂移变成乱拖尾。
- **Drag 采样间隔**：越小轨迹越密，但数据量越大。

不同型号触控板的尺寸和采样率不同，不存在适合所有设备的唯一参数。

### 6. 开始录制

1. 确认状态灯正常。
2. 手指先离开触控板，避免在开始瞬间留下半个手势。
3. 点击“开始录制”。
4. 音乐会从头播放。
5. 浏览器会尝试隐藏并锁定鼠标光标。
6. 跟着音乐在触控板上操作。

录制期间不要关闭 TouchBridge 原生窗口，也不要刷新网页。

### 7. 停止录制

最稳妥的方式是点击红色“停止录制”按钮。

- 浏览器可能先用一次 Esc 退出 Pointer Lock；丢失 Pointer Lock **不会**自动停止录制。
- 如果按 Esc 后页面仍显示“录制中”，请点击“停止录制”。
- 确认“录制中”显示为“否”后再导出。

### 8. 暂停与继续的注意事项

录制期间暂停音乐会冻结音乐时间。继续触摸可能让多个音符堆在同一时间点。因此建议：

1. 先点“停止录制”；
2. 再暂停或调整音乐；
3. 需要重录时先决定是否保留旧音符；
4. 不保留旧音符就点“清空”；
5. 再开始新一轮录制。

每次点击“开始录制”都会把音乐定位到开头，但已有音符不会自动清空。直接再次录制可能与第一遍音符重叠。

### 9. 检查预览

观察音符数量、时间位置和类型。如果出现：

- 音符堆成一团：检查 BPM、Offset、是否在暂停音乐时继续录制；
- 同一次点击重复：增加去抖时间；
- Tap 经常变 Hold：提高 Hold 最短时间；
- Hold 变 Drag：提高死区或调整 Drag 最小速度；
- Flick 识别困难：降低 Flick 速度阈值或扩大 Flick 时间窗口。

### 10. 自动校准

先按节拍录制一小段，再点击“自动校准”。程序会根据音符相对最近整拍的偏差估算采样补偿。

- 正值：音符整体延后；
- 负值：音符整体提前。

自动校准假设你主要点击整拍。如果谱面主要使用反拍或复杂节奏，建议手动调节。

### 11. 导出

停止录制后点击“导出 RPE”。输出目录：

```text
backend/exports/
```

也可以点击网页中的“打开导出文件夹”。

导出的 `.pez` 通常包含：

- RPE 谱面 JSON；
- 音乐；
- 曲绘或占位图；
- `info.txt`；
- `info.yml`。

请在目标编辑器或播放器中实际导入测试。重要谱面建议同时保留多个版本，不要只保存最后一次导出。

---

## 温馨提示

1. **正式打歌前先录 10～20 秒测试。**确认状态灯、音符数、BPM、Offset 和手势类型都正常。
2. **不要在录制中关闭原生采集器。**网页仍可能显示，但不会再有触控输入。
3. **不要在录制中刷新网页。**刷新会影响前端录制控制和音乐时钟。
4. **暂停音乐前先停止录制。**否则后续触摸可能挤在同一时间点。
5. **重录前注意清空。**开始新录制不会自动删除上一遍音符。
6. **光标锁定失效不等于录制停止。**以页面“录制中”状态为准。
7. **红色关闭按钮会真正退出。**本项目不会自动复活被用户关闭的程序。
8. **不要公开上传受版权保护的音乐或曲绘。**GitHub 仓库只应放代码和允许分发的素材。
9. **不要提交 `backend/uploads`、`backend/exports` 或 `backend/venv`。**它们已写入 `.gitignore`。
10. **系统升级前备份可用环境。**私有接口可能在新系统中变化。
11. **USB、网盘和跨系统解压可能丢失执行权限。**遇到问题请重新运行前文的 `chmod` 命令。
12. **不要把“触控板数据”绿灯与真实触摸混为一谈。**绿灯表示采集器心跳正常；实际点击还应看到触控帧、原始 OSC 和音符数变化。

---

## 常见问题与排查

### 网页打不开

检查 8765 端口：

```bash
lsof -nP -iTCP:8765 -sTCP:LISTEN
tail -n 100 /tmp/touchbridge_rpe.log
```

重新双击启动器通常会清理本项目的旧后端。

### OSC 灯不亮

检查 9000 端口：

```bash
lsof -nP -iUDP:9000
```

确认网页端口和原生 TouchBridge 端口都为 9000。

### 触控板数据灯不亮

1. 确认 TouchBridge 原生窗口仍在运行。
2. 检查输入监控权限。
3. 退出采集器后重新启动整个项目。
4. 不要只刷新网页；原生采集器也必须重新打开。

### 一开始能打，后面没输入

当前维护版已处理：

- macOS App Nap 导致采集定时器被暂停；
- 高频 OSC 每包创建线程；
- 浏览器监视消息过多；
- 预览反复传输整张谱；
- Pointer Lock 丢失后静默停止录制；
- 幽灵触点长期占用槽位。

若仍然复现，请不要立刻重启。先记录页面“录制中”状态、三个状态灯、音符数和发生时间，再提交 Issue。

### 停止后再次录制没有新音符

确认：

- 页面“录制中”为“是”；
- 触控板数据灯为绿色；
- 音乐确实在播放而不是暂停；
- 没有在同一时间点重复录制导致预览重叠；
- 必要时先停止、清空，再重新开始。

### 关闭后又自动打开

1.1.1 不包含自动重启守护进程。红色关闭按钮会真正退出。如果旧测试版本仍自动打开，请删除旧目录并重新下载最新版，避免同时运行两个副本。

### 音符整体偏早或偏晚

使用“自动校准”或手动调整采样补偿。蓝牙耳机等设备可能带来额外音频延迟。

### 音符位置不准确

本项目使用 0～1 归一化坐标，不直接依赖触控板物理毫米尺寸。不同设备仍可能因为边缘区域、手势习惯和采样率产生差异，请调整死区和映射参数。

### Python 依赖安装失败

```bash
cd backend
source venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r ../requirements.txt
```

检查网络、Python 版本和目录写入权限。

### 如何查看健康状态

项目运行时访问：

```text
http://127.0.0.1:8765/health
```

其中可看到 OSC 包数、原生帧、触点、槽位和录制状态，方便 Issue 排查。

---

## 测试与开发

运行后端测试：

```bash
python3 backend/test_engine.py
```

编译采集器：

```bash
bash TouchBridge/build.sh
```

原生触控板诊断：

```bash
TouchBridge/TouchBridge.app/Contents/MacOS/TouchBridge --trackpadtest
cat /tmp/touchbridge_trackpad_test.log
```

OSC 自检：

```bash
TouchBridge/TouchBridge.app/Contents/MacOS/TouchBridge --osctest
```

当前版本通过：

- Python 手势与导出单元测试；
- x86_64 编译；
- arm64 交叉编译；
- 6,000+ OSC 包压力测试；
- 停止后再次录制测试；
- 后台 App Nap 连续心跳测试；
- Intel MacBookPro16,1 真实触控测试。

---

## 目录结构

```text
TouchBridge-RPE-plus/
├── README.md
├── NOTICE.md
├── CHANGELOG.md
├── LICENSE
├── VERSION
├── requirements.txt
├── run.command
├── TouchBridge RPE.app/       # 一键启动入口，不包含预编译采集器
├── TouchBridge/
│   ├── main.swift
│   ├── Info.plist
│   └── build.sh
├── backend/
│   ├── app.py
│   ├── engine.py
│   ├── config.json
│   └── test_engine.py
└── frontend/
    ├── index.html
    └── socket.io.min.js
```

运行后会生成但不会提交：

```text
TouchBridge/TouchBridge.app/
backend/venv/
backend/uploads/
backend/exports/
```

---

## 原作者、维护者与来源

### 上游项目

- 原作者 / 上游维护者：[@MiGuo2787](https://github.com/MiGuo2787)
- 上游仓库：[MiGuo2787/TouchBridge-RPE](https://github.com/MiGuo2787/TouchBridge-RPE)

### 当前维护版

- 整合、修复与维护：**Eric_SXR**（GitHub：[@ericsxr-local](https://github.com/ericsxr-local)）
- 当前仓库：`ericsxr-local/TouchBridge-RPE-plus`

### 来源说明

本维护版不是从零原创：

1. 基于上游 TouchBridge-RPE 项目继续开发；
2. 触控读取方式基于社区常用的 `MultitouchSupport.framework` 私有接口实践；
3. RPE、Phira、Phigros 相关格式与名称归各自项目和权利人；
4. 当前维护者主要完成启动、稳定性、OSC 生命周期、性能、光标、导出和文档等修复与整合。

详见 [NOTICE.md](NOTICE.md)。转载、修改和发布时请保留 MIT License、上游来源及维护说明。

---

## 隐私与安全

- Web 仅绑定 `127.0.0.1:8765`。
- OSC 仅绑定 `127.0.0.1:9000`。
- 音乐、曲绘、谱面默认只保存在本机项目目录。
- 启动脚本首次安装 Python 依赖时需要访问 Python 包源。
- 项目不会要求 GitHub 密码、Apple ID 密码或系统管理员密码；系统安装开发工具时可能由 macOS 自己请求确认。

---

## 免责声明

1. 本软件按“现状”提供，不附带任何明示或暗示保证。
2. 私有 API 可能随系统更新失效。
3. 使用者自行承担数据丢失、兼容性、版权与平台规则风险。
4. 本项目与 Phigros、Phira、RPE、PhiEdit 官方没有隶属或合作关系。
5. 维护者无法保证解决每台 Mac 的问题。

---

## License

[MIT License](LICENSE)

发布、修改或分发时，请保留许可证与 [NOTICE.md](NOTICE.md) 中的来源说明。
