# MeetingScribe · 会议实时转写

**全程本地离线运行的会议实时语音转写系统** —— 边说边转写、自动区分说话人、声纹库实名识别、会议管理、AI 一键生成会议纪要。

![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue)
![Python](https://img.shields.io/badge/python-3.12-green)
![License](https://img.shields.io/badge/license-MIT-orange)
![Release](https://img.shields.io/badge/release-v1.0.0-brightgreen)

> 录音、转写、声纹、会议数据全部保存在本机，**运行时不做任何云端调用**。
> 唯一的联网行为是可选的「AI 纪要」功能，也可以指向本地大模型（llama.cpp）。

## 界面预览

| 主界面 | 会议进行中（实时转写） |
|:---:|:---:|
| ![主界面](docs/screenshots/01-main.png) | ![实时转写](docs/screenshots/05-live.png) |

| 历史会议详情 | 说话人卡片 | 设置 |
|:---:|:---:|:---:|
| ![历史会议](docs/screenshots/02-history.png) | ![说话人卡片](docs/screenshots/03-speaker-card.png) | ![设置](docs/screenshots/04-settings.png) |

## 功能特性

- **实时转写**：每句话结束约 1 秒内上屏，带标点与数字归一化，纯 CPU 即可流畅运行
- **说话人分离**：声纹嵌入 + 实时聚类，自动区分「说话人 1/2/3…」；会议结束后后台自动重算校正，准确率进一步提升
- **声纹库**：把说话人登记为实名人员，之后每场会议自动识别、直接显示真实姓名
- **AI 会议纪要**：一键生成结构化纪要（讨论要点 / 决议 / 行动项），支持本地 llama.cpp（默认）或任意 OpenAI 兼容云端 API，界面内直接配置
- **会议管理**：开始 / 暂停 / 继续 / 结束；历史会议回看、回听录音、下载存档
- **会议存档**：每场会议自动保存原始录音（16kHz WAV）+ 逐句结构化转写 + 带时间轴文本
- **原生桌面客户端**：WebView2 内核，无需打开浏览器；自动识别 USB 麦克风阵列（如 reSpeaker）

## 安装

前往 [Releases](../../releases) 下载，两种方式任选（均已内置 Python 运行时与全部本地模型，无需安装任何环境）：

- **安装包 `MeetingScribe-Setup-1.0.0.exe`**：双击安装（需一次管理员授权），可选桌面快捷方式，自带卸载程序
- **便携版 `MeetingScribe-Portable-1.0.0.zip`**：解压后双击 `启动会议转写.bat` 即可运行

> Windows SmartScreen 若提示「未知发布者」，点击「仍要运行」即可（个人自签名证书）。

## 使用

1. 插入 USB 麦克风（推荐 reSpeaker 阵列麦克风，普通笔记本麦克风也能用）
2. 启动「会议转写」，等待右上角变为 **● 已就绪**
3. 左侧点击 **▶ 开始会议**，说话即可看到逐句转写与说话人区分
4. 点击说话人头像 → 改成真实姓名并「登记到声纹库」，下次开会自动识别
5. **⏹ 结束会议** 后自动生成时间轴纪要，可回听录音、生成 AI 纪要、下载存档

## 数据存储

所有个人数据（会议存档、声纹库、配置）保存在 **「文档」文件夹下的 `MeetingScribe` 目录**，与程序文件完全分离：卸载时默认保留个人数据，重装 / 覆盖安装 / 换便携版均不影响。

## 技术栈

SenseVoice（ASR）· ERes2Net（声纹）· Silero VAD · pyannote segmentation（会后重分离）· sherpa-onnx（CPU 推理）· FastAPI + WebSocket · WebView2 客户端 · Inno Setup 打包

## 从源码运行

```bash
git clone https://github.com/Titor99/MeetingScribe.git
cd MeetingScribe
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

下载模型（约 270 MB，放入 `models/` 目录，国内可用 `hf-mirror.com` 加速）：

```bash
# 1. Silero VAD（2 MB）
curl -L -o models/silero_vad.onnx \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx

# 2. SenseVoice int8 ASR（228 MB）— HuggingFace: csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17
#    下载 model.int8.onnx → models/sense-voice.int8.onnx
#    下载 tokens.txt      → models/sense-voice-tokens.txt

# 3. ERes2Net 声纹嵌入（38 MB，下载后重命名）
curl -L -o models/eres2net_speaker.onnx \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_base_200k_sv_zh-cn_16k-common.onnx

# 4. pyannote segmentation 3.0 int8（会后重分离用）
#    HuggingFace: csukuangfj/sherpa-onnx-pyannote-segmentation-3-0
#    下载 model.int8.onnx → models/sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx
```

启动：

```bash
# 桌面客户端（推荐）
.venv\Scripts\python.exe desktop_app.py

# 或仅启动后端，浏览器访问 http://localhost:7100/
.venv\Scripts\python.exe backend\server.py --port 7100
```

首次启动会自动在「文档\MeetingScribe」下生成 `config.json`，按需修改 LLM 地址等配置（参考 `config.example.json`）。

## License

[MIT](LICENSE) © 2026 Titor99

模型各自遵循其原始许可：SenseVoice（MIT）、Silero VAD（MIT）、pyannote segmentation（MIT）、3DSpeaker ERes2Net（Apache-2.0）。
