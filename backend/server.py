#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
会议实时转写系统 - 后端服务
全本地离线运行：音频采集 -> VAD -> SenseVoice ASR -> eres2net 说话人分离 -> WebSocket 推送
"""
import argparse
import asyncio
import json
import queue
import shutil
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

import sys
if getattr(sys, "frozen", False):
    # PyInstaller 打包后：以 exe 所在目录为应用根目录
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"
FRONTEND_DIR = ROOT / "frontend"


def _documents_dir() -> Path:
    """解析当前用户的「文档」目录（读注册表，兼容 OneDrive  Known Folder 重定向）。"""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as k:
            p = winreg.QueryValueEx(k, "Personal")[0]
            if p:
                return Path(p)
    except Exception:
        pass
    return Path.home() / "Documents"


# 个人数据根目录：文档\MeetingScribe（会议存档 / 声纹库 / 个人配置，卸载时默认保留）
USER_DATA_DIR = _documents_dir() / "MeetingScribe"
DATA_DIR = USER_DATA_DIR / "meetings"
CONFIG_PATH = USER_DATA_DIR / "config.json"
VP_PATH = USER_DATA_DIR / "voiceprints.json"   # 固定声纹库


def _migrate_legacy_data():
    """旧版本个人数据在安装目录下（data/ 与 config.json），首次启动自动迁移到文档目录。"""
    legacy_dir = ROOT / "data"
    try:
        legacy_meetings = legacy_dir / "meetings"
        if legacy_meetings.is_dir():
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            for d in sorted(legacy_meetings.iterdir()):
                tgt = DATA_DIR / d.name
                if not tgt.exists():
                    shutil.move(str(d), str(tgt))
            try:
                legacy_meetings.rmdir()  # 已全部搬走则删除空目录
            except OSError:
                pass
        legacy_vp = legacy_dir / "voiceprints.json"
        if legacy_vp.exists() and not VP_PATH.exists():
            USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy_vp), str(VP_PATH))
        legacy_cfg = ROOT / "config.json"
        if legacy_cfg.exists() and not CONFIG_PATH.exists():
            USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy_cfg), str(CONFIG_PATH))
        try:
            if legacy_dir.exists() and not any(legacy_dir.iterdir()):
                legacy_dir.rmdir()
        except OSError:
            pass
    except Exception:
        pass


_migrate_legacy_data()

SAMPLE_RATE = 16000
VAD_WINDOW = 512  # silero v4 @16kHz

DEFAULT_CONFIG = {
    "vad_threshold": 0.5,
    "min_speech_duration": 0.25,
    "min_silence_duration": 0.5,
    "max_speech_duration": 15.0,
    "speaker_threshold": 0.58,       # 余弦相似度，低于则判定为新说话人
    "voiceprint_threshold": 0.6,     # 与声纹库匹配的相似度阈值（高于则识别为库中人员）
    "speaker_change_split": True,    # 长语音段内部再做说话人切换切分
    "num_threads": 4,
    "device_keyword": "reSpeaker",   # 自动匹配的录音设备名关键字
    "llm": {
        "base_url": "http://127.0.0.1:8080",  # LLM 服务的 OpenAI 兼容地址（本地默认 llama.cpp）
        "api_key": "",               # API 密钥；本地服务一般留空，云端服务在此填写
        "model": "",                 # 留空则自动取服务器上已加载的模型
        "temperature": 0.3,
        "max_tokens": 4096,          # 单次生成最大 token 数
        "chunk_chars": 6000,         # 转写文本超过该长度则分块摘要后汇总
        "request_timeout": 900,      # 单次请求超时（秒），27B 本地推理较慢
    },
    "diarization": {
        # 会后离线重分离：pyannote segmentation 3.0 + eres2net 声纹聚类
        "seg_model": "sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx",  # 相对 models/ 目录（int8 量化版，CPU 上快约 2-3 倍）
        "threshold": 0.7,            # 自动模式聚类阈值（远场会议建议指定人数而非自动）
        "merge_threshold": 0.65,     # 自动模式：质心相似度高于该值的小簇被合并
        "min_cluster_seconds": 3.0,  # 自动模式：总时长小于该值的簇视为碎片并入最相近簇
        "auto_rediarize": True,      # 会议结束后自动对全程录音做离线重分离（混合式：实时标签+会后校正）
    },
}

SPEAKER_COLORS = [
    "#4fc3f7", "#ffb74d", "#aed581", "#f06292", "#ba68c8",
    "#4db6ac", "#fff176", "#a1887f", "#90a4ae", "#e57373",
]


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            user_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for key in ("llm", "diarization"):
                sub = user_cfg.pop(key, None)
                if isinstance(sub, dict):  # 子配置段做深合并，缺省键保留默认值
                    merged = dict(DEFAULT_CONFIG[key])
                    merged.update(sub)
                    cfg[key] = merged
                else:
                    cfg[key] = dict(DEFAULT_CONFIG[key])
            cfg.update(user_cfg)
        except Exception:
            pass
    else:
        # 首次运行：生成一份默认配置到 文档\MeetingScribe\config.json，便于用户查阅修改
        try:
            USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        except Exception:
            pass
    return cfg


# ---------------------------------------------------------------- 模型层
class Models:
    """懒加载本地模型：VAD / SenseVoice ASR / eres2net 说话人嵌入"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.loaded = False
        self.error = None
        self.recognizer = None
        self.extractor = None
        self._diarizer = None
        self._diarizer_key = None

    def load(self):
        import sherpa_onnx
        try:
            self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(MODELS_DIR / "sense-voice.int8.onnx"),
                tokens=str(MODELS_DIR / "sense-voice-tokens.txt"),
                num_threads=self.cfg["num_threads"],
                use_itn=True,
                language="zh",  # 锁定中文：避免短语气词被误判成日语（对比实测发现）
                debug=False,
            )
            self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
                sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                    model=str(MODELS_DIR / "eres2net_speaker.onnx"),
                    num_threads=self.cfg["num_threads"],
                    debug=False,
                )
            )
            self._sherpa = sherpa_onnx
            self.loaded = True
        except Exception as e:  # noqa
            self.error = str(e)
            raise

    def new_vad(self):
        cfg = sherpa_cfg = self._sherpa.VadModelConfig()
        cfg.silero_vad.model = str(MODELS_DIR / "silero_vad.onnx")
        cfg.silero_vad.threshold = self.cfg["vad_threshold"]
        cfg.silero_vad.min_speech_duration = self.cfg["min_speech_duration"]
        cfg.silero_vad.min_silence_duration = self.cfg["min_silence_duration"]
        cfg.silero_vad.max_speech_duration = self.cfg["max_speech_duration"]
        cfg.sample_rate = SAMPLE_RATE
        return self._sherpa.VoiceActivityDetector(cfg, buffer_size_in_seconds=3600)

    def transcribe(self, samples: np.ndarray) -> str:
        stream = self.recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        self.recognizer.decode_stream(stream)
        return stream.result.text.strip()

    def embedding(self, samples: np.ndarray) -> np.ndarray:
        stream = self.extractor.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        stream.input_finished()
        emb = np.array(self.extractor.compute(stream), dtype=np.float32)
        n = np.linalg.norm(emb)
        return emb / n if n > 0 else emb

    def diarizer(self, num_clusters=None):
        """懒加载离线说话人分离器（pyannote 分割 + eres2net 嵌入 + 聚类）。
        num_clusters 为空时按阈值自动聚类；聚类参数变化时自动重建。"""
        dcfg = self.cfg.get("diarization", {})
        th = dcfg.get("threshold", 0.7)
        key = (num_clusters or 0, th)
        if self._diarizer is not None and self._diarizer_key == key:
            return self._diarizer
        so = self._sherpa
        seg_model = MODELS_DIR / dcfg.get("seg_model", "")
        if not seg_model.exists():
            raise RuntimeError(f"离线分离模型不存在：{seg_model}")
        cfg = so.OfflineSpeakerDiarizationConfig(
            segmentation=so.OfflineSpeakerSegmentationModelConfig(
                pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=str(seg_model)),
                num_threads=self.cfg["num_threads"], debug=False,
            ),
            embedding=so.SpeakerEmbeddingExtractorConfig(
                model=str(MODELS_DIR / "eres2net_speaker.onnx"),
                num_threads=self.cfg["num_threads"], debug=False,
            ),
            clustering=so.FastClusteringConfig(
                num_clusters=num_clusters or -1, threshold=th),
            min_duration_on=0.2, min_duration_off=0.5,
        )
        if not cfg.validate():
            raise RuntimeError(f"离线分离配置无效：{cfg}")
        self._diarizer = so.OfflineSpeakerDiarization(cfg)
        self._diarizer_key = key
        return self._diarizer


# ---------------------------------------------------------------- LLM 会议纪要
MINUTES_SYSTEM = (
    "/no_think\n"
    "你是一名专业的中文会议纪要助手。你的任务是根据会议实时语音转写文本，"
    "整理出结构清晰、忠实原文的会议纪要。要求：\n"
    "1. 只依据提供的转写内容，不得编造转写中不存在的事实、决议或承诺；\n"
    "2. 转写文本由语音识别产生，可能存在同音错别字，请在理解语义的基础上合理纠正，但不要改变原意；\n"
    "3. 引用关键结论时附上对应的时间戳（格式 [HH:MM:SS]），方便回听录音定位；\n"
    "4. 行动项若转写中未明确负责人或时限，标注“未明确”，不要臆测。"
)

MINUTES_FINAL_TEMPLATE = """\
会议信息：
- 会议名称：{name}
- 会议时间：{created_at}
- 录音时长：{duration}
- 参会情况：{speakers}

以下是本次会议的完整转写记录（格式：[时间戳] 说话人：内容）：

{dialogue}

请根据以上转写内容输出会议纪要，使用 Markdown 格式，结构如下：
# 会议纪要：{name}
## 一、会议概况
（一段话概述会议主题、讨论背景与整体结论）
## 二、讨论要点
（按主题归纳各方观点，标注是谁提出的，附时间戳）
## 三、决议事项
（会议达成的明确结论/决定；若没有则写“本次未形成明确决议”）
## 四、行动项
（Markdown 表格：| 事项 | 负责人 | 时限 | 时间戳 |；没有行动项则写“无”）
## 五、遗留问题与后续跟进
（未决问题、约定下次讨论的内容；没有则写“无”）
"""

CHUNK_EXTRACT_TEMPLATE = """\
以下是会议转写记录的第 {idx}/{total} 部分（格式：[时间戳] 说话人：内容）：

{chunk}

请提取这一部分的关键信息，用简洁的中文列点输出：
- 讨论要点（谁提出了什么观点，附时间戳）
- 达成的决议或结论
- 行动项（事项、负责人、时限；未明确则标注“未明确”）
- 遗留问题
只依据原文，不要编造。输出控制在 400 字以内。
"""


class LLMClient:
    """对接 OpenAI 兼容接口的 LLM 服务（本地 llama.cpp 或云端，仅用标准库）"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.base = cfg.get("base_url", "http://127.0.0.1:8080").rstrip("/")
        self.api_key = cfg.get("api_key", "").strip()

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _get(self, path, timeout=2):
        req = urllib.request.Request(self.base + path, headers=self._headers())
        return urllib.request.urlopen(req, timeout=timeout)

    def available(self) -> bool:
        for path in ("/health", "/v1/models"):
            try:
                with self._get(path, timeout=2) as r:
                    if r.status == 200:
                        return True
            except Exception:
                continue
        return False

    def model_name(self) -> str:
        if self.cfg.get("model"):
            return self.cfg["model"]
        try:
            with self._get("/v1/models", timeout=3) as r:
                data = json.loads(r.read().decode("utf-8"))
                items = data.get("data") or []
                if items:
                    return items[0].get("id", "local-model")
        except Exception:
            pass
        return "local-model"

    def chat_stream(self, messages, max_tokens=None, temperature=None):
        """流式调用 /v1/chat/completions，逐 token yield 文本内容"""
        payload = {
            "model": self.model_name(),
            "messages": messages,
            "temperature": self.cfg.get("temperature", 0.3) if temperature is None else temperature,
            "max_tokens": max_tokens or self.cfg.get("max_tokens", 4096),
            "stream": True,
            "chat_template_kwargs": {"enable_thinking": False},  # Qwen3 关闭思考模式
        }
        req = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        timeout = self.cfg.get("request_timeout", 900)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            buf = b""
            while True:
                chunk = resp.read(1)
                if not chunk:
                    break
                buf += chunk
                if chunk != b"\n":
                    continue
                line = buf.decode("utf-8", errors="ignore").strip()
                buf = b""
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                for ch in obj.get("choices", []):
                    delta = ch.get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        yield piece

    def chat(self, messages, max_tokens=None) -> str:
        """非流式（内部通过流式拼接实现，避免超长响应超时）"""
        return "".join(self.chat_stream(messages, max_tokens=max_tokens))


def _fmt_hms(t):
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def build_dialogue(segs, speakers):
    """把逐句转写整理成紧凑的对话文本：同人相邻短句合并，减少 token 消耗"""
    spk = {s["id"]: s.get("label", f"说话人 {s['id']}") for s in speakers}
    lines = []
    for s in segs:
        label = s.get("speaker_label") or spk.get(s.get("speaker_id"), f"说话人 {s.get('speaker_id')}")
        text = (s.get("text") or "").strip()
        if not text:
            continue
        # 与 finalize 相同的合并规则：同人、间隔 <1s 的连续发言并入一行
        if (lines and lines[-1][0] == label
                and s["start"] - lines[-1][2] < 1.0):
            lines[-1][1] += text
            lines[-1][2] = s["end"]
        else:
            lines.append([label, text, s["end"], s["start"]])
    return "\n".join(f"[{_fmt_hms(l[3])}] {l[0]}：{l[1]}" for l in lines)


def split_dialogue(dialogue: str, chunk_chars: int):
    """按行（说话人轮次）边界把长对话切成若干块，每块不超过 chunk_chars"""
    lines = dialogue.split("\n")
    chunks, cur, cur_len = [], [], 0
    for ln in lines:
        if cur and cur_len + len(ln) + 1 > chunk_chars:
            chunks.append("\n".join(cur))
            cur, cur_len = [], 0
        cur.append(ln)
        cur_len += len(ln) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


class MinutesGenerator:
    """会议结束后的纪要生成：短会直接一次生成，长会分块提取再汇总（map-reduce）"""

    def __init__(self, llm: LLMClient, cfg):
        self.llm = llm
        self.cfg = cfg

    def generate(self, meta, segs, emit):
        """emit(str) 逐段输出最终纪要文本；返回完整纪要"""
        name = meta.get("name", "未命名会议")
        speakers = meta.get("speakers") or []
        dialogue = build_dialogue(segs, speakers)
        if not dialogue.strip():
            raise RuntimeError("本场会议没有有效的转写内容，无法生成纪要")

        spk_desc = "、".join(
            f"{s.get('label', '说话人')}（发言 {s.get('count', 0)} 次 / {round(s.get('seconds', 0))} 秒）"
            for s in speakers) or "未识别到说话人"
        info = {
            "name": name,
            "created_at": (meta.get("created_at") or "").replace("T", " "),
            "duration": _fmt_hms(meta.get("recorded_seconds", 0)),
            "speakers": spk_desc,
        }

        chunk_chars = int(self.cfg.get("chunk_chars", 6000))
        if len(dialogue) <= chunk_chars:
            body = dialogue
        else:
            chunks = split_dialogue(dialogue, chunk_chars)
            notes = []
            for i, ch in enumerate(chunks, 1):
                msgs = [
                    {"role": "system", "content": MINUTES_SYSTEM},
                    {"role": "user", "content": CHUNK_EXTRACT_TEMPLATE.format(
                        idx=i, total=len(chunks), chunk=ch)},
                ]
                note = self.llm.chat(msgs, max_tokens=1200).strip()
                notes.append(f"### 第 {i} 部分要点\n{note}")
            body = ("（会议较长，以下为分段提取的要点汇总，已保留全部关键信息）\n\n"
                    + "\n\n".join(notes))

        final_msgs = [
            {"role": "system", "content": MINUTES_SYSTEM},
            {"role": "user", "content": MINUTES_FINAL_TEMPLATE.format(dialogue=body, **info)},
        ]
        full = []
        for piece in self.llm.chat_stream(final_msgs):
            full.append(piece)
            emit(piece)
        result = "".join(full).strip()
        if not result:
            raise RuntimeError("大模型没有返回内容，请检查 llama-server 状态")
        return result


# ---------------------------------------------------------------- 会后离线重分离
def load_wav_mono(path) -> np.ndarray:
    """读取 WAV 为 16k 单声道 float32"""
    with wave.open(str(path), "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw == 1:
        d = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128) / 128.0
    elif sw == 2:
        d = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    else:
        d = np.frombuffer(raw, np.int32).astype(np.float32) / 2147483648.0
    if ch > 1:
        d = d.reshape(-1, ch).mean(axis=1)
    if sr != SAMPLE_RATE:
        import soxr
        d = soxr.resample(d, sr, SAMPLE_RATE)
    return d


def cluster_centroids(turns, samples, models, max_sec=20.0):
    """每个簇拼接至多 max_sec 秒音频计算声纹质心，返回 {簇号: 嵌入向量}"""
    sps = []
    for _, _, sp in turns:
        if sp not in sps:
            sps.append(sp)
    cents = {}
    for sp in sps:
        bufs, total = [], 0.0
        for st, et, s in turns:
            if s != sp or total >= max_sec:
                continue
            a = int(st * SAMPLE_RATE)
            b = int(min(et, st + (max_sec - total)) * SAMPLE_RATE)
            if b > a:
                bufs.append(samples[a:b])
                total += (b - a) / SAMPLE_RATE
        if bufs:
            cents[sp] = models.embedding(np.concatenate(bufs))
    return cents


def merge_fragment_clusters(turns, samples, models, dcfg, cents=None):
    """自动聚类清理：总时长过短的碎片簇，若与某大簇声纹质心足够相似则并入；
    相似度不够时保留为独立说话人（宁多勿错并）。cents 可传入已算好的质心避免重复提取"""
    min_sec = dcfg.get("min_cluster_seconds", 3.0)
    merge_th = dcfg.get("merge_threshold", 0.65)
    dur = {}
    for st, et, sp in turns:
        dur[sp] = dur.get(sp, 0.0) + (et - st)
    big = [sp for sp, d in dur.items() if d >= min_sec]
    if len(big) <= 1 or len(dur) == len(big):
        return turns
    if cents is None:
        cents = cluster_centroids(turns, samples, models)
    remap = {}
    for sp in dur:
        if sp in big or sp not in cents:
            continue
        best, best_sim = None, -1.0
        for bp in big:
            if bp not in cents:
                continue
            sim = float(np.dot(cents[sp], cents[bp]))
            if sim > best_sim:
                best, best_sim = bp, sim
        if best is not None and best_sim >= merge_th:
            remap[sp] = best
    if not remap:
        return turns
    return [(st, et, remap.get(sp, sp)) for st, et, sp in turns]


def assign_turn_speakers(segs, turns):
    """按时间重叠度给每句转写分配分离簇号，返回 {seg_index: cluster}"""
    out = {}
    ti = 0
    for i, seg in enumerate(segs):
        s0, s1 = seg["start"], seg["end"]
        best_sp, best_ov = None, 0.0
        # turns 按开始时间排序，双指针前移
        while ti + 1 < len(turns) and turns[ti][1] <= s0:
            ti += 1
        j = ti
        while j < len(turns) and turns[j][0] < s1:
            st, et, sp = turns[j]
            ov = min(et, s1) - max(st, s0)
            if ov > best_ov:
                best_ov, best_sp = ov, sp
            j += 1
        out[i] = best_sp  # 无重叠时为 None，保留原标注
    return out


# ---------------------------------------------------------------- 固定声纹库
class VoiceprintStore:
    """已注册人员的固定声纹库，data/voiceprints.json 持久化，全离线"""

    def __init__(self):
        VP_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.entries = []
        self.dirty = False       # 会话自适应更新过质心，会议结束时落盘
        if VP_PATH.exists():
            try:
                for e in json.loads(VP_PATH.read_text(encoding="utf-8")):
                    e["centroid"] = np.array(e["centroid"], dtype=np.float32)
                    self.entries.append(e)
            except Exception:
                self.entries = []

    def _save(self):
        data = [{**e, "centroid": [round(float(x), 6) for x in e["centroid"]]}
                for e in self.entries]
        VP_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
        self.dirty = False

    def save_if_dirty(self):
        if self.dirty:
            self._save()

    def add(self, name, centroid: np.ndarray, source=""):
        # 同名人员：融合进已有声纹（多会议样本，提高泛化），而不是重复建档
        for e in self.entries:
            if e["name"] == name:
                c = e["centroid"] * 0.5 + centroid * 0.5
                e["centroid"] = c / (np.linalg.norm(c) + 1e-9)
                e["source"] = (e.get("source", "") + "；" + source).strip("；")
                self._save()
                return e
        entry = {
            "id": uuid.uuid4().hex[:8],
            "name": name,
            "color": SPEAKER_COLORS[len(self.entries) % len(SPEAKER_COLORS)],
            "centroid": centroid.astype(np.float32),
            "source": source,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        self.entries.append(entry)
        self._save()
        return entry

    def delete(self, vid):
        before = len(self.entries)
        self.entries = [e for e in self.entries if e["id"] != vid]
        if len(self.entries) != before:
            self._save()
            return True
        return False

    def rename(self, vid, name):
        return self.update(vid, name=name)

    def update(self, vid, name=None, color=None, role=None):
        """更新声纹库人员的姓名 / 颜色 / 角色（任一可空，空则不改动）"""
        for e in self.entries:
            if e["id"] == vid:
                if name is not None and name.strip():
                    e["name"] = name.strip()
                if color:
                    e["color"] = color
                if role is not None:
                    e["role"] = role.strip()
                self._save()
                return e
        return None

    def match(self, emb: np.ndarray):
        """返回 (最佳匹配 entry, 相似度)；库为空返回 (None, 0)"""
        best, bs = None, 0.0
        for e in self.entries:
            sim = float(np.dot(emb, e["centroid"]))
            if sim > bs:
                best, bs = e, sim
        return best, bs

    def public(self):
        return [{"id": e["id"], "name": e["name"], "color": e["color"],
                 "role": e.get("role", ""),
                 "source": e.get("source", ""), "created_at": e.get("created_at", "")}
                for e in self.entries]


# ---------------------------------------------------------------- 说话人注册
class SpeakerRegistry:
    def __init__(self, threshold, new_speaker_min_dur=1.2):
        self.threshold = threshold
        self.new_speaker_min_dur = new_speaker_min_dur
        self.speakers = []      # {id,label,color,centroid,count,seconds,vp_id?}
        self.enrolled = {}      # vp_id -> speaker dict

    def reset(self):
        self.speakers = []
        self.enrolled = {}

    def assign_enrolled(self, vp: dict) -> dict:
        """命中声纹库人员：同一会话内复用同一说话人槽位，质心固定不漂移"""
        if vp["id"] in self.enrolled:
            return self.enrolled[vp["id"]]
        idx = len(self.speakers)
        sp = {
            "id": idx + 1,
            "label": vp["name"],
            "color": vp["color"],
            "centroid": vp["centroid"],
            "count": 0,
            "seconds": 0.0,
            "vp_id": vp["id"],
        }
        self.speakers.append(sp)
        self.enrolled[vp["id"]] = sp
        return sp

    def assign(self, emb: np.ndarray, dur: float = 99.0) -> dict:
        """陌生人自动聚类：只与自动簇比较（已注册人员由声纹库先行匹配）"""
        best_i, best_sim = -1, -1.0
        for i, sp in enumerate(self.speakers):
            if sp.get("vp_id"):
                continue
            sim = float(np.dot(emb, sp["centroid"]))
            if sim > best_sim:
                best_sim, best_i = sim, i
        # 短语音声纹不可靠：不允许新建说话人，就近归入
        may_create = dur >= self.new_speaker_min_dur
        if best_i < 0 or (best_sim < self.threshold and may_create):
            idx = len(self.speakers)
            sp = {
                "id": idx + 1,
                "label": f"说话人 {idx + 1}",
                "color": SPEAKER_COLORS[idx % len(SPEAKER_COLORS)],
                "centroid": emb,
                "count": 0,
                "seconds": 0.0,
            }
            self.speakers.append(sp)
            return sp
        if best_i < 0:
            # 极端情况：只有已注册人员且都不匹配 → 建第一个自动簇
            return self.assign_enrolled_fallback(emb)
        sp = self.speakers[best_i]
        # 质心缓慢更新，避免漂移导致同人拆分
        c = sp["centroid"] * 0.9 + emb * 0.1
        sp["centroid"] = c / (np.linalg.norm(c) + 1e-9)
        return sp

    def assign_enrolled_fallback(self, emb):
        idx = len(self.speakers)
        sp = {
            "id": idx + 1, "label": f"说话人 {idx + 1}",
            "color": SPEAKER_COLORS[idx % len(SPEAKER_COLORS)],
            "centroid": emb, "count": 0, "seconds": 0.0,
        }
        self.speakers.append(sp)
        return sp

    def assign_nearest(self, emb, vp=None, vp_sim=0.0, vp_threshold=0.5):
        """短句专用：绝不新建说话人。优先以放宽阈值归入注册人，否则就近归入任意已有说话人"""
        if vp is not None and vp_sim >= vp_threshold:
            return self.assign_enrolled(vp)
        best, bs = None, -1.0
        for sp in self.speakers:
            sim = float(np.dot(emb, sp["centroid"]))
            if sim > bs:
                best, bs = sp, sim
        if best is not None:
            return best
        return self.assign_enrolled_fallback(emb)

    def merge_similar(self, merge_threshold=None):
        """会议结束后全局校正：只对陌生人自动簇做合并；已注册人员不合并、不改名。
        返回 old_id -> new_id 映射（编号重排为连续值）"""
        th = merge_threshold or self.threshold
        auto = [s for s in self.speakers if not s.get("vp_id")]
        enrolled = [s for s in self.speakers if s.get("vp_id")]
        # 自动簇合并
        clusters = []
        cl_of = {}
        for sp in auto:
            placed = None
            for k, cl in enumerate(clusters):
                if float(np.dot(sp["centroid"], cl)) >= th:
                    placed = k
                    break
            if placed is None:
                placed = len(clusters)
                clusters.append(sp["centroid"])
            cl_of[sp["id"]] = placed
        # 重排编号：已注册人员优先（保持出现顺序），其后为自动簇
        mapping = {}
        merged = []
        nid = 0
        for sp in enrolled:
            nid += 1
            mapping[sp["id"]] = nid
            merged.append({"id": nid, "label": sp["label"], "color": sp["color"],
                           "role": sp.get("role", ""),
                           "count": sp["count"], "seconds": sp["seconds"],
                           "vp_id": sp["vp_id"], "centroid": sp["centroid"]})
        for k in range(len(clusters)):
            nid += 1
            members = [s for s in auto if cl_of[s["id"]] == k]
            for s in members:
                mapping[s["id"]] = nid
            merged.append({"id": nid, "label": f"说话人 {nid}",
                           "color": SPEAKER_COLORS[(nid - 1) % len(SPEAKER_COLORS)],
                           "role": next((s.get("role") for s in members if s.get("role")), ""),
                           "count": sum(s["count"] for s in members),
                           "seconds": sum(s["seconds"] for s in members),
                           "centroid": clusters[k]})
        self.speakers = merged
        self.enrolled = {s["vp_id"]: s for s in merged if s.get("vp_id")}
        return mapping

    def public(self):
        return [
            {"id": s["id"], "label": s["label"], "color": s["color"],
             "role": s.get("role", ""), "vp_id": s.get("vp_id"),
             "count": s["count"], "seconds": round(s["seconds"], 1)}
            for s in self.speakers
        ]


# ---------------------------------------------------------------- 处理流水线
class Pipeline(threading.Thread):
    """音频队列消费线程：VAD 分段 -> 说话人切分/嵌入 -> ASR -> 广播"""

    def __init__(self, models: Models, cfg, broadcast_cb, on_segment_cb):
        super().__init__(daemon=True)
        self.models = models
        self.cfg = cfg
        self.broadcast = broadcast_cb          # fn(dict) 推送给前端
        self.on_segment = on_segment_cb        # fn(seg_dict, samples) 持久化
        self.q = queue.Queue(maxsize=400)
        self.recording = False
        self.registry = SpeakerRegistry(cfg["speaker_threshold"],
                                        cfg.get("new_speaker_min_duration", 1.2))
        self.voiceprints = None  # 由 Server 注入
        self.vad = None
        self.base_offset = 0.0    # 已记录时长（暂停恢复后累加）
        self.seg_seq = 0
        self.running = True

    # ---- 外部命令（全部经队列保证顺序）----
    def start_recording(self):
        self.registry.reset()
        self.base_offset = 0.0
        self.seg_seq = 0
        self.q.put(("start", None))

    def pause(self):
        self.q.put(("pause", None))

    def resume(self):
        self.q.put(("resume", None))

    def stop(self):
        self.stopped_event = threading.Event()
        self.q.put(("stop", None))
        return self.stopped_event

    def feed(self, chunk: np.ndarray):
        try:
            self.q.put_nowait(("audio", chunk))
        except queue.Full:
            # 处理速度跟不上时音频被丢弃（WAV 录音在更上游写入，不受影响）
            self._drop_count = getattr(self, "_drop_count", 0) + 1
            now = time.time()
            if now - getattr(self, "_drop_warn_ts", 0.0) > 10:
                self._drop_warn_ts = now
                self.broadcast({"type": "warn", "message":
                                f"处理速度跟不上，已有部分语音未被转写（累计 {self._drop_count} 块），"
                                "录音文件完整不受影响。建议调大 config.json 的 num_threads 或缩短 max_speech_duration。"})

    # ---- 内部 ----
    def _drain_vad(self, out_segments):
        while not self.vad.empty():
            s = self.vad.front
            start = s.start / SAMPLE_RATE
            samples = np.array(s.samples, dtype=np.float32).copy()
            self.vad.pop()
            out_segments.append((start, samples))

    def _split_by_speaker(self, samples: np.ndarray):
        """对较长语音段做说话人切换点检测，返回 [(offset_sec, sub_samples)]"""
        if not self.cfg["speaker_change_split"] or len(samples) < SAMPLE_RATE * 5:
            return [(0.0, samples)]
        win, hop = int(SAMPLE_RATE * 2.0), int(SAMPLE_RATE * 0.5)
        embs, marks = [], []
        pos = 0
        while pos + int(SAMPLE_RATE * 0.8) <= len(samples):
            w = samples[pos:pos + win]
            embs.append(self.models.embedding(w))
            marks.append(pos)
            pos += hop
        if len(embs) < 3:
            return [(0.0, samples)]
        boundaries = [0]
        for i in range(1, len(embs)):
            if float(np.dot(embs[i], embs[i - 1])) < self.cfg["speaker_threshold"] - 0.06:
                boundaries.append(marks[i])
        boundaries.append(len(samples))
        pieces = []
        for a, b in zip(boundaries[:-1], boundaries[1:]):
            if b - a >= int(SAMPLE_RATE * 0.5):
                pieces.append((a / SAMPLE_RATE, samples[a:b]))
        return pieces or [(0.0, samples)]

    def _process_segment(self, rel_start: float, samples: np.ndarray):
        if len(samples) < SAMPLE_RATE * 0.3:
            return
        vp_th = self.cfg.get("voiceprint_threshold", 0.6)
        min_new_dur = self.cfg.get("new_speaker_min_duration", 1.2)
        for sub_off, sub in self._split_by_speaker(samples):
            dur = len(sub) / SAMPLE_RATE
            emb = self.models.embedding(sub)
            sp = None
            vp, sim = None, 0.0
            # 1) 查固定声纹库（0.5 秒以上都参与匹配；长句在阈值边界附近也归入注册人）
            if self.voiceprints and self.voiceprints.entries and dur >= 0.5:
                vp, sim = self.voiceprints.match(emb)
                if vp and (sim >= vp_th or (dur >= 1.5 and sim >= vp_th - 0.07)):
                    sp = self.registry.assign_enrolled(vp)
                    # 会话自适应：命中后把本次声纹少量混入注册质心，适应当前设备/环境
                    if sim >= vp_th:
                        c = vp["centroid"] * 0.97 + emb * 0.03
                        vp["centroid"] = c / (np.linalg.norm(c) + 1e-9)
                        self.voiceprints.dirty = True
            # 2) 短句绝不新建说话人：就近归入（含注册人，阈值略放宽）
            if sp is None and dur < min_new_dur:
                sp = self.registry.assign_nearest(
                    emb, vp=vp, vp_sim=sim, vp_threshold=vp_th - 0.10)
            # 3) 陌生人自动聚类
            if sp is None:
                sp = self.registry.assign(emb, dur=dur)
            text = self.models.transcribe(sub)
            if not text:
                continue
            st = self.base_offset + rel_start + sub_off
            et = st + len(sub) / SAMPLE_RATE
            sp["count"] += 1
            sp["seconds"] += et - st
            self.seg_seq += 1
            seg = {
                "id": self.seg_seq,
                "speaker_id": sp["id"],
                "speaker_label": sp["label"],
                "speaker_color": sp["color"],
                "start": round(st, 2),
                "end": round(et, 2),
                "text": text,
            }
            self.on_segment(seg)
            self.broadcast({"type": "segment", "segment": seg,
                            "speakers": self.registry.public()})

    def _flush(self):
        if self.vad is None:
            return
        segs = []
        self.vad.flush()
        self._drain_vad(segs)
        for st, samples in segs:
            self._process_segment(st, samples)

    def run(self):
        segs = []
        speech_active = False
        last_level = 0.0
        while self.running:
            try:
                kind, payload = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            if kind == "start":
                self.vad = self.models.new_vad()
                self.recording = True
            elif kind == "pause":
                self._flush()
                self.recording = False
                self.broadcast({"type": "speech_active", "active": False})
            elif kind == "resume":
                self.vad = self.models.new_vad()
                self.recording = True
            elif kind == "stop":
                self._flush()
                self.recording = False
                self.broadcast({"type": "speech_active", "active": False})
                if hasattr(self, "stopped_event"):
                    self.stopped_event.set()
            elif kind == "audio":
                chunk = payload
                now = time.time()
                if now - last_level > 0.2:
                    rms = float(np.sqrt(np.mean(chunk ** 2) + 1e-12))
                    self.broadcast({"type": "level", "rms": round(rms, 4),
                                    "recording": self.recording})
                    last_level = now
                if not self.recording or self.vad is None:
                    continue
                self.vad.accept_waveform(chunk)
                segs.clear()
                self._drain_vad(segs)
                for st, samples in segs:
                    self._process_segment(st, samples)
                active = not self.vad.empty() or self._vad_talking()
                if active != speech_active:
                    speech_active = active
                    self.broadcast({"type": "speech_active", "active": active})

    def _vad_talking(self):
        # silero 内部检测到有未结束的语音段
        try:
            return self.vad.is_speech_detected()
        except Exception:
            return False


# ---------------------------------------------------------------- 会议存储
class MeetingStore:
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    def sweep_stale(self):
        """启动时清理僵尸会议：状态停留在 recording/paused 的会议说明
        上次服务未正常结束（进程已退出，内存状态不可恢复，无法续会），
        统一标记为已结束、补全 transcript.txt，并修复可能损坏的 WAV 头"""
        fixed = 0
        for d in sorted(DATA_DIR.iterdir()):
            if not d.is_dir():
                continue
            meta = self.get_meta(d.name)
            if not meta or meta.get("status") not in ("recording", "paused"):
                continue
            segs = self.transcript(d.name)
            meta["status"] = "ended"
            meta["ended_at"] = datetime.fromtimestamp(
                d.stat().st_mtime).isoformat(timespec="seconds")
            if segs:
                # 以最后一条转写为准补全统计（原记录可能因中途退出而不准）
                meta["segment_count"] = len(segs)
                meta["recorded_seconds"] = round(
                    max(meta.get("recorded_seconds", 0), segs[-1].get("end", 0)), 1)
            self.save_meta(meta)
            self._repair_wav(d / "audio.wav")
            if segs:
                try:
                    self.finalize(d.name)
                except Exception:
                    pass
            fixed += 1
        return fixed

    @staticmethod
    def _repair_wav(p):
        """进程被杀时 WAV 头可能未写完（帧数记为 0），按文件实际大小重写头"""
        if not p.exists():
            return
        try:
            with wave.open(str(p), "rb") as w:
                if w.getnframes() > 0:
                    return  # 头完好
        except Exception:
            pass
        try:
            data = p.read_bytes()
            if len(data) <= 44:
                return
            payload = data[44:]
            with wave.open(str(p), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(payload)
        except Exception:
            pass

    @staticmethod
    def _dir(mid):
        # 防路径穿越：会议目录必须在 DATA_DIR 下
        d = (DATA_DIR / mid).resolve()
        if d.parent != DATA_DIR.resolve():
            raise RuntimeError("非法的会议 ID")
        return d

    def rename_meeting(self, mid, name):
        """重命名会议：更新 meta.json，并重建 transcript.txt 让标题同步"""
        name = (name or "").strip()
        if not name:
            raise RuntimeError("会议名称不能为空")
        meta = self.get_meta(mid)
        if not meta:
            raise RuntimeError("会议不存在")
        meta["name"] = name
        self.save_meta(meta)
        if (self._dir(mid) / "transcript.txt").exists():
            self.finalize(mid)
        return meta

    def delete_meeting(self, mid):
        d = self._dir(mid)
        if not d.exists():
            raise RuntimeError("会议不存在")
        shutil.rmtree(d)

    def create(self, name):
        mid = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        d = self._dir(mid)
        d.mkdir(parents=True, exist_ok=True)
        meta = {
            "id": mid,
            "name": name or datetime.now().strftime("会议 %Y-%m-%d %H:%M"),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "status": "recording",
            "recorded_seconds": 0.0,
            "speakers": [],
            "segment_count": 0,
        }
        (d / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        (d / "transcript.jsonl").touch()
        return meta

    def get_meta(self, mid):
        p = self._dir(mid) / "meta.json"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def save_meta(self, meta):
        (self._dir(meta["id"]) / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def append_segment(self, mid, seg):
        with open(self._dir(mid) / "transcript.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(seg, ensure_ascii=False) + "\n")

    def remap_speakers(self, mid, mapping: dict):
        """按 old_id -> new_id 映射重写 transcript.jsonl 中的说话人编号"""
        p = self._dir(mid) / "transcript.jsonl"
        if not p.exists():
            return
        lines = []
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            seg = json.loads(line)
            if seg.get("speaker_id") in mapping:
                seg["speaker_id"] = mapping[seg["speaker_id"]]
            lines.append(json.dumps(seg, ensure_ascii=False))
        p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def transcript(self, mid):
        p = self._dir(mid) / "transcript.jsonl"
        if not p.exists():
            return []
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]

    # ---- AI 纪要 ----
    def save_summary(self, mid, content):
        (self._dir(mid) / "summary.md").write_text(content, encoding="utf-8")
        meta = self.get_meta(mid)
        if meta:
            meta["has_summary"] = True
            meta["summary_at"] = datetime.now().isoformat(timespec="seconds")
            self.save_meta(meta)

    def read_summary(self, mid):
        p = self._dir(mid) / "summary.md"
        return p.read_text(encoding="utf-8") if p.exists() else None

    def apply_rediarization(self, mid, segs, speakers):
        """离线重分离落盘：备份原转写（首次），写入新标注，重建 meta 与 transcript.txt"""
        d = self._dir(mid)
        p = d / "transcript.jsonl"
        bak = d / "transcript.jsonl.bak"
        if p.exists() and not bak.exists():
            shutil.copy2(p, bak)  # 只备份一次，保留最初的实时版本
        with open(p, "w", encoding="utf-8") as f:
            for seg in segs:
                f.write(json.dumps(seg, ensure_ascii=False) + "\n")
        meta = self.get_meta(mid)
        meta["speakers"] = speakers
        meta["segment_count"] = len(segs)
        meta["rediarized_at"] = datetime.now().isoformat(timespec="seconds")
        if meta.get("has_summary"):
            meta["summary_stale"] = True  # 说话人变了，旧 AI 纪要可能过时
        self.save_meta(meta)
        self.finalize(mid)
        return meta

    def rename_speaker(self, mid, speaker_id, name):
        return self.update_speaker(mid, speaker_id, name=name)

    def update_speaker(self, mid, speaker_id, name=None, color=None, role=None):
        """更新某位说话人的姓名 / 颜色 / 角色：同步 meta.json 与 transcript.jsonl"""
        if name is not None:
            name = name.strip()
            if not name:
                raise RuntimeError("姓名不能为空")
        meta = self.get_meta(mid)
        if not meta:
            raise RuntimeError("会议不存在")
        found = False
        for s in meta.get("speakers", []):
            if s.get("id") == speaker_id:
                if name is not None:
                    s["label"] = name
                if color:
                    s["color"] = color
                if role is not None:
                    s["role"] = role.strip()
                found = True
        if not found:
            raise RuntimeError(f"说话人 {speaker_id} 不存在")
        self.save_meta(meta)
        p = self._dir(mid) / "transcript.jsonl"
        if p.exists():
            lines = []
            for line in p.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                seg = json.loads(line)
                if seg.get("speaker_id") == speaker_id:
                    if name is not None:
                        seg["speaker_label"] = name
                    if color:
                        seg["speaker_color"] = color
                lines.append(json.dumps(seg, ensure_ascii=False))
            p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        # transcript.txt 一并重建，保证下载的纪要也是新名字
        if (self._dir(mid) / "transcript.txt").exists():
            self.finalize(mid)
        return meta

    def list_all(self):
        out = []
        if DATA_DIR.exists():
            for d in sorted(DATA_DIR.iterdir(), reverse=True):
                m = self.get_meta(d.name)
                if m:
                    m["has_audio"] = (d / "audio.wav").exists()
                    out.append(m)
        return out

    def finalize(self, mid):
        meta = self.get_meta(mid)
        segs = self.transcript(mid)
        lines = [f"{meta['name']}", f"会议时间：{meta['created_at']}",
                 f"录音时长：{meta['recorded_seconds']:.0f} 秒　"
                 f"说话人数：{len(meta['speakers'])}　发言条数：{meta['segment_count']}",
                 "=" * 48]
        spk_map = {s["id"]: s for s in meta["speakers"]}

        def fmt(t):
            t = int(t)
            return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"

        prev_spk, prev_end = None, None
        for s in segs:
            label = spk_map.get(s["speaker_id"], {}).get("label", f"说话人 {s['speaker_id']}")
            if s["speaker_id"] == prev_spk and prev_end is not None and s["start"] - prev_end < 1.0:
                lines[-1] += s["text"]
            else:
                lines.append(f"[{fmt(s['start'])}] {label}：{s['text']}")
            prev_spk, prev_end = s["speaker_id"], s["end"]
        (self._dir(mid) / "transcript.txt").write_text(
            "\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------- 音频引擎
def list_input_devices():
    """只列出 WASAPI 接口的物理输入设备，按名称去重（每个物理设备只出现一次）"""
    import sounddevice as sd
    wasapi = None
    for i, h in enumerate(sd.query_hostapis()):
        if "WASAPI" in h["name"]:
            wasapi = i
            break
    out, seen = [], set()
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] <= 0:
            continue
        if wasapi is not None and d["hostapi"] != wasapi:
            continue
        name = d["name"].strip()
        if name in seen:
            continue
        seen.add(name)
        out.append({"index": i, "name": name, "rate": int(d["default_samplerate"])})
    return out


class AudioEngine(threading.Thread):
    """采集线程：麦克风（sounddevice）或测试文件，统一输出 16k 单声道 float32"""

    def __init__(self, pipeline: Pipeline):
        super().__init__(daemon=True)
        self.pipeline = pipeline
        self.cmd = queue.Queue()
        self.stream = None
        self.device_name = None
        self.mode = "idle"  # idle | mic | file
        self._stop_flag = threading.Event()

    def _find_device(self, keyword=None, device_index=None):
        import sounddevice as sd
        devices = list_input_devices()
        if device_index is not None:
            d = sd.query_devices(device_index)
            return device_index, d["name"], int(d["default_samplerate"])
        if keyword:
            for d in devices:
                if keyword.lower() in d["name"].lower():
                    return d["index"], d["name"], d["rate"]
        if devices:
            d = devices[0]
            return d["index"], d["name"], d["rate"]
        idx = sd.default.device[0]
        d = sd.query_devices(idx)
        return idx, d["name"], int(d["default_samplerate"])

    def start_mic(self, keyword=None, device_index=None):
        self.cmd.put(("mic", {"keyword": keyword, "device_index": device_index}))

    def start_file(self, path, speed=1.0):
        self.cmd.put(("file", {"path": path, "speed": speed}))

    def idle(self):
        self.cmd.put(("idle", None))

    def _open_mic(self, keyword, device_index):
        """按优先级尝试打开设备：指定设备 → 同名设备的其他接口 → 关键字匹配 → 其余输入设备。
        每个候选都实际试开流，失败自动换下一个，全部失败才报错。"""
        import sounddevice as sd
        import soxr
        apis = sd.query_hostapis()
        api_order = {"Windows WASAPI": 0, "MME": 1, "Windows DirectSound": 2,
                     "Windows WDM-KS": 3}

        def api_rank(i):
            return api_order.get(apis[sd.query_devices(i)["hostapi"]]["name"], 9)

        candidates = []
        if device_index is not None:
            candidates.append(device_index)
        # 与指定设备同名的其他接口（防止索引错位）
        if device_index is not None:
            want = sd.query_devices(device_index)["name"].strip().lower()
            for i, d in enumerate(sd.query_devices()):
                if d["max_input_channels"] > 0 and d["name"].strip().lower() == want \
                        and i not in candidates:
                    candidates.append(i)
        # 关键字匹配
        if keyword:
            ks = [i for i, d in enumerate(sd.query_devices())
                  if d["max_input_channels"] > 0
                  and keyword.lower() in d["name"].lower()
                  and i not in candidates]
            ks.sort(key=api_rank)
            candidates += ks
        # 兜底：所有输入设备
        rest = [i for i, d in enumerate(sd.query_devices())
                if d["max_input_channels"] > 0 and i not in candidates]
        rest.sort(key=api_rank)
        candidates += rest

        errors = []
        for idx in candidates:
            d = sd.query_devices(idx)
            rate = int(d["default_samplerate"])
            if rate < 16000:
                continue
            for ch in (1, 2):
                if ch > d["max_input_channels"]:
                    continue
                rs = soxr.ResampleStream(rate, SAMPLE_RATE, 1, quality="HQ") \
                    if rate != SAMPLE_RATE else None

                def cb(indata, frames, t, status, rs=rs):
                    mono = indata.mean(axis=1).astype(np.float32)
                    if rs is not None:
                        mono = rs.resample_chunk(mono, last=False)
                    if len(mono):
                        self.pipeline.feed(mono)

                try:
                    stream = sd.InputStream(device=idx, channels=ch, samplerate=rate,
                                            blocksize=int(rate * 0.03), callback=cb)
                    stream.start()
                    self.stream = stream
                    self.device_name = d["name"]
                    self.mode = "mic"
                    return
                except Exception as e:
                    errors.append(f"[{idx}] {d['name']} ch={ch}: {e}")
        raise RuntimeError("所有候选录音设备均打开失败。最后一次错误："
                           + (errors[-1] if errors else "无可用设备"))

    def _run_file(self, path, speed):
        import soxr
        with wave.open(path, "rb") as w:
            sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
        if sw == 1:
            data = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128) / 128.0
        elif sw == 2:
            data = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
        else:
            data = np.frombuffer(raw, np.int32).astype(np.float32) / 2147483648.0
        if ch > 1:
            data = data.reshape(-1, ch).mean(axis=1)
        if sr != SAMPLE_RATE:
            data = soxr.resample(data, sr, SAMPLE_RATE)
        self.device_name = f"测试文件 {Path(path).name}"
        self.mode = "file"
        step = int(SAMPLE_RATE * 0.03 * 4)  # 120ms 一块
        i = 0
        while i < len(data) and self.mode == "file":
            self.pipeline.feed(data[i:i + step])
            i += step
            time.sleep(step / SAMPLE_RATE / max(speed, 0.1))
        if self.mode == "file":
            self.mode = "idle"
            self.pipeline.broadcast({"type": "file_finished"})

    def _close_stream(self):
        if self.stream is not None:
            try:
                self.stream.stop(); self.stream.close()
            except Exception:
                pass
            self.stream = None

    def run(self):
        cur = None
        while not self._stop_flag.is_set():
            try:
                kind, payload = self.cmd.get(timeout=0.1)
            except queue.Empty:
                continue
            if kind == "mic":
                if self.mode == "file":
                    self.mode = "idle"  # 让文件循环退出
                else:
                    self._close_stream()
                    try:
                        self._open_mic(payload.get("keyword"), payload.get("device_index"))
                        self.pipeline.broadcast(
                            {"type": "device", "name": self.device_name})
                    except Exception as e:
                        self.pipeline.broadcast(
                            {"type": "error", "message": f"打开录音设备失败：{e}"})
            elif kind == "file":
                self._close_stream()
                self.mode = "file"
                threading.Thread(target=self._run_file,
                                 args=(payload["path"], payload.get("speed", 1.0)),
                                 daemon=True).start()
            elif kind == "idle":
                self._close_stream()
                self.mode = "idle"


# ---------------------------------------------------------------- 服务主体
class Server:
    def __init__(self):
        self.cfg = load_config()
        self.models = Models(self.cfg)
        self.store = MeetingStore()
        self.clients = set()
        self.loop = None
        self.meeting = None          # 当前进行中会议的 meta
        self.meeting_started_ts = 0.0
        self.acc_seconds = 0.0       # 已记录秒数（暂停前累计）
        self.pipeline = Pipeline(self.models, self.cfg, self.broadcast_sync, self._on_segment)
        self.voiceprints = VoiceprintStore()
        self.pipeline.voiceprints = self.voiceprints
        self.engine = AudioEngine(self.pipeline)
        self.llm = LLMClient(self.cfg.get("llm", {}))
        self.minutes = MinutesGenerator(self.llm, self.cfg.get("llm", {}))
        stale = self.store.sweep_stale()
        if stale:
            print(f"[store] 已将 {stale} 个未正常结束的会议标记为已结束", flush=True)
        self.pipeline.start()
        self.engine.start()

    # ---- 广播 ----
    def broadcast_sync(self, msg: dict):
        if self.loop:
            self.loop.call_soon_threadsafe(self._put, msg)

    def _put(self, msg):
        for q in list(self.clients):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    def _on_segment(self, seg):
        if self.meeting:
            self.store.append_segment(self.meeting["id"], seg)
            self.meeting["segment_count"] += 1

    def _sync_speakers(self):
        if self.meeting:
            self.meeting["speakers"] = self.pipeline.registry.public()
            self.meeting["recorded_seconds"] = round(self._recorded_seconds(), 1)
            self.store.save_meta(self.meeting)

    def _recorded_seconds(self):
        t = self.acc_seconds
        if self.meeting and self.meeting["status"] == "recording":
            t += time.time() - self.meeting_started_ts
        return t

    # ---- 业务操作 ----
    def start_meeting(self, name=None, device_index=None, source="mic",
                      file_path=None, speed=1.0):
        if not self.models.loaded:
            raise RuntimeError("模型尚未加载完成，请稍候")
        if self.meeting and self.meeting["status"] in ("recording", "paused"):
            raise RuntimeError("已有会议正在进行，请先结束")
        meta = self.store.create(name)
        self.meeting = meta
        self.acc_seconds = 0.0
        self.meeting_started_ts = time.time()
        self.pipeline.start_recording()
        if source == "file":
            self.engine.start_file(file_path, speed)
        else:
            self.engine.start_mic(self.cfg["device_keyword"], device_index)
        self.broadcast_sync({"type": "meeting", "action": "started", "meeting": meta})
        return meta

    def pause_meeting(self):
        self._require_active()
        if self.meeting["status"] != "recording":
            raise RuntimeError("会议未在录音中")
        self.pipeline.pause()
        self.acc_seconds += time.time() - self.meeting_started_ts
        self.meeting["status"] = "paused"
        self._sync_speakers()
        self.broadcast_sync({"type": "meeting", "action": "paused", "meeting": self.meeting})

    def resume_meeting(self):
        self._require_active()
        if self.meeting["status"] != "paused":
            raise RuntimeError("会议未处于暂停状态")
        self.meeting_started_ts = time.time()
        self.meeting["status"] = "recording"
        self.pipeline.base_offset = self.acc_seconds
        self.pipeline.resume()
        self._sync_speakers()
        self.broadcast_sync({"type": "meeting", "action": "resumed", "meeting": self.meeting})

    def end_meeting(self):
        self._require_active()
        ev = self.pipeline.stop()
        ev.wait(timeout=60)  # 等待队列中剩余音频处理完毕
        if self.meeting["status"] == "recording":
            self.acc_seconds += time.time() - self.meeting_started_ts
        self.meeting["recorded_seconds"] = round(self.acc_seconds, 1)
        self.meeting["status"] = "ended"
        self.meeting["ended_at"] = datetime.now().isoformat(timespec="seconds")
        # 会后全局校正：合并声纹相近的说话人（修复实时过程中的过度拆分）
        merge_th = self.cfg.get("speaker_merge_threshold",
                                self.cfg["speaker_threshold"])
        mapping = self.pipeline.registry.merge_similar(merge_th)
        if len(set(mapping.values())) < len(mapping):
            self.store.remap_speakers(self.meeting["id"], mapping)
        self.voiceprints.save_if_dirty()  # 会话自适应的声纹更新落盘
        self._sync_speakers()
        self.store.finalize(self.meeting["id"])
        m = self.meeting
        self.meeting = None
        self.pipeline.base_offset = 0.0
        self.acc_seconds = 0.0
        self.broadcast_sync({"type": "meeting", "action": "ended", "meeting": m})
        return m

    def update_llm_settings(self, base_url, api_key, model):
        """更新 LLM 配置（界面设置项）：内存生效 + 持久化到 config.json"""
        c = self.cfg["llm"]
        if base_url is not None:
            base_url = base_url.strip().rstrip("/")
            if base_url and not base_url.startswith(("http://", "https://")):
                raise RuntimeError("服务地址需以 http:// 或 https:// 开头")
            if base_url:
                c["base_url"] = base_url
        if api_key is not None:
            c["api_key"] = api_key.strip()
        if model is not None:
            c["model"] = model.strip()
        try:
            disk = {}
            if CONFIG_PATH.exists():
                disk = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            disk["llm"] = c
            CONFIG_PATH.write_text(json.dumps(disk, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        except Exception as e:
            raise RuntimeError(f"配置写入失败：{e}")
        self.llm.base = c["base_url"].rstrip("/")
        self.llm.api_key = c["api_key"]
        ok = self.llm.available()
        return {"available": ok, "base_url": self.llm.base,
                "model": self.llm.model_name() if ok else c.get("model", "")}

    def maybe_auto_rediarize(self, mid):
        """会议结束且 WAV 已关闭后调用：后台对全程录音自动离线重分离，
        用全会议上下文校正实时阶段的过度拆分（混合式标注）"""
        dcfg = self.cfg.get("diarization", {})
        if not dcfg.get("auto_rediarize", True) or not self.models.loaded:
            return

        def work():
            try:
                time.sleep(8)  # 先让结束流程（收尾/总结弹窗）完成，再占用 CPU 做校正
                if not self.store.transcript(mid):
                    return
                self.broadcast_sync({"type": "info",
                                     "message": "会议已结束，正在后台按全程录音校正说话人（不影响使用）…"})
                t0 = time.time()
                result = self.rediarize_meeting(mid)
                n = len(result["speakers"])
                print(f"[auto-rediarize] done in {time.time() - t0:.1f}s, "
                      f"{n} speakers, {result['changed']} changed", flush=True)
                # 附带最新逐字稿，前端可就地刷新已上屏的人名标注
                segments = self.store.transcript(mid)
                if result["changed"] > 0:
                    self.broadcast_sync({
                        "type": "meeting", "action": "updated",
                        "meeting": result["meeting"], "segments": segments,
                        "message": f"说话人已按全程录音重新校正（共 {n} 人，"
                                   f"{result['changed']} 句归属有调整）"})
                else:
                    self.broadcast_sync({"type": "meeting", "action": "updated",
                                         "meeting": result["meeting"],
                                         "segments": segments})
            except Exception as e:
                print(f"[auto-rediarize] {e}", flush=True)

        threading.Thread(target=work, daemon=True).start()

    def _require_active(self):
        if not self.meeting or self.meeting["status"] not in ("recording", "paused"):
            raise RuntimeError("当前没有进行中的会议")

    def rename_live_speaker(self, speaker_id, name):
        return self.update_live_speaker(speaker_id, name=name)

    def update_live_speaker(self, speaker_id, name=None, color=None, role=None):
        """会议进行中实时更新说话人：注册表 + 已产生转写 + 后续新发言全部生效"""
        self._require_active()
        if name is not None:
            name = name.strip()
            if not name:
                raise RuntimeError("姓名不能为空")
        sp = None
        for s in self.pipeline.registry.speakers:
            if s["id"] == speaker_id:
                sp = s
                break
        if not sp:
            raise RuntimeError(f"说话人 {speaker_id} 不存在")
        if name is not None:
            sp["label"] = name
        if color:
            sp["color"] = color
        if role is not None:
            sp["role"] = role.strip()
        mid = self.meeting["id"]
        self._sync_speakers()  # meta 里的说话人列表先同步（含新名字/颜色/角色）
        try:
            self.store.update_speaker(mid, speaker_id, name=name, color=color, role=role)
        except RuntimeError:
            pass  # meta 里还没有该说话人时仅更新注册表即可
        self.meeting = self.store.get_meta(mid)
        self.broadcast_sync({"type": "speaker_renamed", "speaker_id": speaker_id,
                             "name": sp["label"], "color": sp["color"],
                             "role": sp.get("role", ""),
                             "speakers": self.pipeline.registry.public()})
        return sp

    def generate_minutes(self, mid, emit):
        """为已结束的会议生成 AI 纪要；emit(str) 流式回调。返回完整纪要文本"""
        meta = self.store.get_meta(mid)
        if not meta:
            raise RuntimeError("会议不存在")
        if not self.llm.available():
            raise RuntimeError("LLM_OFFLINE")
        segs = self.store.transcript(mid)
        result = self.minutes.generate(meta, segs, emit)
        self.store.save_summary(mid, result)
        meta = self.store.get_meta(mid)
        if meta and meta.pop("summary_stale", None) is not None:
            self.store.save_meta(meta)  # 纪要已按新标注重生成，清除过期标记
        return result

    def rediarize_meeting(self, mid, num_clusters=None):
        """会后离线重分离：对完整录音重算说话人时间轴，按重叠度给每句转写重打标签"""
        if not self.models.loaded:
            raise RuntimeError("模型尚未加载完成，请稍候")
        if self.meeting and self.meeting["id"] == mid \
                and self.meeting["status"] in ("recording", "paused"):
            raise RuntimeError("会议正在进行中，结束后才能重新分离")
        meta = self.store.get_meta(mid)
        if not meta:
            raise RuntimeError("会议不存在")
        wav_path = DATA_DIR / mid / "audio.wav"
        if not wav_path.exists():
            raise RuntimeError("该会议没有录音文件，无法重新分离")
        segs = self.store.transcript(mid)
        if not segs:
            raise RuntimeError("该会议没有转写内容")

        samples = load_wav_mono(wav_path)
        sd = self.models.diarizer(num_clusters)
        result = sd.process(samples).sort_by_start_time()
        turns = [(r.start, r.end, r.speaker) for r in result]
        if not turns:
            raise RuntimeError("离线分离未识别到任何语音")
        # 质心只算一遍：碎片簇清理与声纹库匹配共用（原来是两遍全量嵌入提取，耗时翻倍）
        cents = cluster_centroids(turns, samples, self.models)
        if not num_clusters:  # 自动模式：清理碎片簇
            turns = merge_fragment_clusters(
                turns, samples, self.models, self.cfg.get("diarization", {}), cents=cents)

        # 簇号 → 新说话人编号（按首次出现顺序）
        order = []
        for _, _, sp in turns:
            if sp not in order:
                order.append(sp)
        new_no = {sp: i + 1 for i, sp in enumerate(order)}

        # 按时间重叠给每句转写分配新说话人
        assign = assign_turn_speakers(segs, turns)
        old_labels = {s["id"]: s.get("label", "") for s in meta.get("speakers", [])}
        old_colors = {s["id"]: s.get("color", "") for s in meta.get("speakers", [])}

        # 名字保留：统计新簇与旧说话人的时间重叠，贪心如一对应则沿用旧名字
        overlap = {}  # new_no -> {old_id: seconds}
        for i, seg in enumerate(segs):
            sp = assign.get(i)
            if sp is None:
                continue
            nn = new_no[sp]
            oid = seg.get("speaker_id")
            d = seg["end"] - seg["start"]
            overlap.setdefault(nn, {}).setdefault(oid, 0.0)
            overlap[nn][oid] += d
        used_labels, label_of = set(), {}
        color_of = {}
        for nn in sorted(overlap, key=lambda k: -sum(overlap[k].values())):
            best = max(overlap[nn].items(), key=lambda kv: kv[1])
            # 颜色沿用主导旧说话人的颜色（保留用户在卡片里的自定义配色）
            if old_colors.get(best[0]):
                color_of[nn] = old_colors[best[0]]
            lbl = old_labels.get(best[0], "")
            # 仅沿用用户手动改过的名字；自动生成的“说话人 N”不沿用（否则编号与名字错位）
            is_custom = lbl and lbl != f"说话人 {best[0]}"
            if is_custom and lbl not in used_labels:
                label_of[nn] = lbl
                used_labels.add(lbl)

        # 重建说话人列表与转写
        stats = {nn: {"count": 0, "seconds": 0.0} for nn in new_no.values()}
        changed = 0
        new_segs = []
        for i, seg in enumerate(segs):
            seg = dict(seg)
            sp = assign.get(i)
            if sp is not None:
                nn = new_no[sp]
                if seg.get("speaker_id") != nn:
                    changed += 1
                stats[nn]["count"] += 1
                stats[nn]["seconds"] += seg["end"] - seg["start"]
                seg["speaker_id"] = nn
            new_segs.append(seg)

        # 丢弃未分配到任何语句的空簇，按首次出现时间重排为连续编号
        keep = [nn for nn in stats if stats[nn]["count"] > 0]
        keep.sort(key=lambda n: min(
            (s["start"] for s in new_segs if s.get("speaker_id") == n),
            default=float("inf")))
        renum = {old: i + 1 for i, old in enumerate(keep)}

        # 声纹库自动命名：簇质心与注册声纹相似度达标则直接用注册名
        # （用户手动改过的名字优先；同一个声纹只分给相似度最高的一个簇）
        vp_label = {}
        cands = []
        for old in keep:
            if old in label_of:
                continue
            sp = order[old - 1]  # new_no 编号 → 原始簇号
            cent = cents.get(sp)
            if cent is None:
                continue
            vp, sim = self.voiceprints.match(cent)
            if vp and sim >= self.cfg["voiceprint_threshold"]:
                cands.append((sim, old, vp))
        used_vp = set()
        for sim, old, vp in sorted(cands, key=lambda x: -x[0]):
            if old not in vp_label and vp["id"] not in used_vp:
                vp_label[old] = vp["name"]
                used_vp.add(vp["id"])

        final_label = {renum[old]: (label_of.get(old) or vp_label.get(old)
                                    or f"说话人 {renum[old]}")
                       for old in keep}
        final_color = {renum[old]: (color_of.get(old)
                                    or SPEAKER_COLORS[(renum[old] - 1) % len(SPEAKER_COLORS)])
                       for old in keep}
        for seg in new_segs:
            nn = seg.get("speaker_id")
            if nn in renum:
                nid = renum[nn]
                seg["speaker_id"] = nid
                seg["speaker_label"] = final_label[nid]
                seg["speaker_color"] = final_color[nid]
        speakers = [{
            "id": nid,
            "label": final_label[nid],
            "color": final_color[nid],
            "count": stats[old]["count"],
            "seconds": round(stats[old]["seconds"], 1),
        } for old, nid in sorted(renum.items(), key=lambda kv: kv[1])]

        meta = self.store.apply_rediarization(mid, new_segs, speakers)
        return {"speakers": speakers, "changed": changed, "total": len(new_segs),
                "meeting": meta}

    def state(self):
        return {
            "models_loaded": self.models.loaded,
            "model_error": self.models.error,
            "device": self.engine.device_name,
            "engine_mode": self.engine.mode,
            "meeting": self.meeting,
            "recorded_seconds": round(self._recorded_seconds(), 1),
            "speakers": self.pipeline.registry.public() if self.meeting else [],
            "llm_base_url": self.llm.base,
            "data_dir": str(USER_DATA_DIR),
            "voiceprints": self.voiceprints.public(),
        }

    # ---- 声纹库 ----
    def enroll_speaker(self, mid, speaker_id, name):
        """把某场会议中的说话人存入固定声纹库，并把该会议中此人改名为注册名"""
        name = (name or "").strip()
        if not name:
            raise RuntimeError("姓名不能为空")
        if not self.models.loaded:
            raise RuntimeError("模型尚未加载完成")
        meta = self.store.get_meta(mid)
        if not meta:
            raise RuntimeError("会议不存在")
        wav_path = DATA_DIR / mid / "audio.wav"
        if not wav_path.exists():
            raise RuntimeError("该会议没有录音文件，无法提取声纹")
        segs = [s for s in self.store.transcript(mid)
                if s.get("speaker_id") == speaker_id]
        if not segs:
            raise RuntimeError(f"该会议中没有说话人 {speaker_id} 的发言")
        samples = load_wav_mono(wav_path)
        # 取时长最长的若干段提取声纹并平均，短时发言跳过
        embs = []
        for s in sorted(segs, key=lambda x: -(x["end"] - x["start"]))[:25]:
            a = int(s["start"] * SAMPLE_RATE)
            b = min(int(s["end"] * SAMPLE_RATE), len(samples))
            if b - a >= int(SAMPLE_RATE * 0.8):
                embs.append(self.models.embedding(samples[a:b]))
        if not embs:
            raise RuntimeError("此人的发言都太短（<0.8 秒），无法提取可靠声纹")
        centroid = np.mean(np.stack(embs), axis=0)
        centroid = centroid / (np.linalg.norm(centroid) + 1e-9)
        vp = self.voiceprints.add(name, centroid, source=meta.get("name", ""))
        # 该会议中此人的标签同步为注册名
        try:
            self.store.rename_speaker(mid, speaker_id, name)
        except Exception:
            pass
        self.broadcast_sync({"type": "voiceprints",
                             "voiceprints": self.voiceprints.public()})
        return {"id": vp["id"], "name": vp["name"], "color": vp["color"],
                "samples_used": len(embs)}


SERVER = None  # 在 main 中赋值


# ---------------------------------------------------------------- FastAPI
def create_app(server: Server):
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel

    app = FastAPI(title="会议实时转写")

    class StartReq(BaseModel):
        name: str | None = None
        device_index: int | None = None
        source: str = "mic"
        file_path: str | None = None
        speed: float = 1.0

    @app.get("/api/status")
    def status():
        return server.state()

    @app.get("/api/heartbeat")
    def heartbeat_get():
        # 客户端看门狗查询最近一次页面前端心跳时间
        return {"last": getattr(server, "last_heartbeat", None)}

    @app.post("/api/heartbeat")
    def heartbeat_post():
        # 前端页面存活心跳：客户端模式（desktop_app）据此判断窗口是否已关闭
        server.last_heartbeat = time.time()
        return {"ok": True}

    @app.get("/api/devices")
    def devices():
        return {"devices": list_input_devices(), "keyword": server.cfg["device_keyword"]}

    @app.post("/api/meeting/start")
    def start(req: StartReq):
        try:
            if req.source == "file":
                if not req.file_path or not Path(req.file_path).exists():
                    raise RuntimeError("测试音频文件不存在")
            m = server.start_meeting(req.name, req.device_index, req.source,
                                     req.file_path, req.speed)
            return {"ok": True, "meeting": m}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    @app.post("/api/meeting/pause")
    def pause():
        try:
            server.pause_meeting()
            return {"ok": True}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    @app.post("/api/meeting/resume")
    def resume():
        try:
            server.resume_meeting()
            return {"ok": True}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    class LiveRenameReq(BaseModel):
        speaker_id: int
        name: str

    @app.post("/api/meeting/rename_speaker")
    def rename_live_speaker(req: LiveRenameReq):
        """会议进行中实时改名"""
        try:
            sp = server.rename_live_speaker(req.speaker_id, req.name)
            return {"ok": True, "speaker": {"id": sp["id"], "label": sp["label"]}}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    class LiveUpdateReq(BaseModel):
        speaker_id: int
        name: str | None = None
        color: str | None = None
        role: str | None = None

    @app.post("/api/meeting/update_speaker")
    def update_live_speaker(req: LiveUpdateReq):
        """会议进行中实时更新说话人姓名 / 颜色 / 角色"""
        try:
            sp = server.update_live_speaker(req.speaker_id, name=req.name,
                                            color=req.color, role=req.role)
            return {"ok": True, "speaker": {"id": sp["id"], "label": sp["label"],
                                            "color": sp["color"], "role": sp.get("role", "")}}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    @app.post("/api/meeting/end")
    def end():
        try:
            m = server.end_meeting()
            return {"ok": True, "meeting": m}
        except Exception as e:
            import traceback; traceback.print_exc()
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    @app.get("/api/meetings")
    def meetings():
        return {"meetings": server.store.list_all()}

    @app.get("/api/meetings/{mid}")
    def meeting_detail(mid: str):
        meta = server.store.get_meta(mid)
        if not meta:
            return JSONResponse({"ok": False, "error": "not found"}, 404)
        return {"meeting": meta, "transcript": server.store.transcript(mid)}

    @app.get("/api/meetings/{mid}/audio")
    def meeting_audio(mid: str):
        p = DATA_DIR / mid / "audio.wav"
        if not p.exists():
            return JSONResponse({"ok": False, "error": "no audio"}, 404)
        return FileResponse(p, media_type="audio/wav", filename=f"{mid}.wav")

    @app.get("/api/meetings/{mid}/transcript.txt")
    def meeting_txt(mid: str):
        p = DATA_DIR / mid / "transcript.txt"
        if not p.exists():
            server.store.finalize(mid)
        return FileResponse(p, media_type="text/plain; charset=utf-8",
                            filename=f"{mid}.txt")

    # ---- AI 会议纪要 ----
    @app.get("/api/llm/status")
    def llm_status():
        return {"available": server.llm.available(), "base_url": server.llm.base}

    @app.get("/api/settings/llm")
    def get_llm_settings():
        c = server.cfg["llm"]
        return {"base_url": c.get("base_url", ""), "api_key": c.get("api_key", ""),
                "model": c.get("model", "")}

    class LlmSettingsReq(BaseModel):
        base_url: str | None = None
        api_key: str | None = None
        model: str | None = None

    @app.post("/api/settings/llm")
    def set_llm_settings(req: LlmSettingsReq):
        try:
            return {"ok": True, **server.update_llm_settings(
                req.base_url, req.api_key, req.model)}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    @app.get("/api/meetings/{mid}/summary")
    def get_summary(mid: str):
        content = server.store.read_summary(mid)
        if content is None:
            return {"has_summary": False}
        return {"has_summary": True, "content": content}

    @app.get("/api/meetings/{mid}/summary.md")
    def download_summary(mid: str):
        p = DATA_DIR / mid / "summary.md"
        if not p.exists():
            return JSONResponse({"ok": False, "error": "no summary"}, 404)
        return FileResponse(p, media_type="text/markdown; charset=utf-8",
                            filename=f"{mid}-会议纪要.md")

    @app.post("/api/meetings/{mid}/summary")
    def gen_summary(mid: str):
        """SSE 流式生成纪要：先发 data: {"token": "..."}，结束发 data: {"done": true}"""
        from fastapi.responses import StreamingResponse

        if server.store.get_meta(mid) is None:
            return JSONResponse({"ok": False, "error": "会议不存在"}, 404)
        if not server.llm.available():
            return JSONResponse({"ok": False, "error": "LLM_OFFLINE",
                                 "base_url": server.llm.base}, 503)

        def stream():
            try:
                q = queue.Queue()
                DONE = object()

                def emit(piece):
                    q.put(piece)

                def worker():
                    try:
                        server.generate_minutes(mid, emit)
                        q.put(DONE)
                    except Exception as e:
                        q.put(e)

                threading.Thread(target=worker, daemon=True).start()
                while True:
                    item = q.get()
                    if item is DONE:
                        yield f"data: {json.dumps({'done': True}, ensure_ascii=False)}\n\n"
                        break
                    if isinstance(item, Exception):
                        yield f"data: {json.dumps({'error': str(item)}, ensure_ascii=False)}\n\n"
                        break
                    yield f"data: {json.dumps({'token': item}, ensure_ascii=False)}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    class RenameReq(BaseModel):
        speaker_id: int
        name: str

    @app.post("/api/meetings/{mid}/rename_speaker")
    def rename_speaker(mid: str, req: RenameReq):
        try:
            meta = server.store.rename_speaker(mid, req.speaker_id, req.name)
            return {"ok": True, "meeting": meta}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    class UpdateSpeakerReq(BaseModel):
        speaker_id: int
        name: str | None = None
        color: str | None = None
        role: str | None = None

    @app.post("/api/meetings/{mid}/update_speaker")
    def update_speaker(mid: str, req: UpdateSpeakerReq):
        """历史会议：更新说话人姓名 / 颜色 / 角色"""
        try:
            meta = server.store.update_speaker(mid, req.speaker_id,
                                               name=req.name, color=req.color, role=req.role)
            return {"ok": True, "meeting": meta}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    class RenameMeetingReq(BaseModel):
        name: str

    @app.post("/api/meetings/{mid}/rename")
    def rename_meeting(mid: str, req: RenameMeetingReq):
        try:
            meta = server.store.rename_meeting(mid, req.name)
            return {"ok": True, "meeting": meta}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    @app.delete("/api/meetings/{mid}")
    def delete_meeting(mid: str):
        try:
            if server.meeting and server.meeting["id"] == mid \
                    and server.meeting["status"] in ("recording", "paused"):
                raise RuntimeError("会议正在进行中，请先结束再删除")
            server.store.delete_meeting(mid)
            return {"ok": True}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    class RediarizeReq(BaseModel):
        num_clusters: int | None = None  # 预期参会人数；留空自动聚类

    @app.post("/api/meetings/{mid}/rediarize")
    def rediarize(mid: str, req: RediarizeReq):
        try:
            result = server.rediarize_meeting(mid, req.num_clusters)
            return {"ok": True, **result}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    # ---- 固定声纹库 ----
    class EnrollReq(BaseModel):
        speaker_id: int
        name: str

    @app.get("/api/voiceprints")
    def list_voiceprints():
        return {"voiceprints": server.voiceprints.public(),
                "threshold": server.cfg.get("voiceprint_threshold", 0.6)}

    @app.post("/api/meetings/{mid}/enroll_speaker")
    def enroll_speaker(mid: str, req: EnrollReq):
        try:
            vp = server.enroll_speaker(mid, req.speaker_id, req.name)
            return {"ok": True, "voiceprint": vp}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    class VpRenameReq(BaseModel):
        name: str

    @app.post("/api/voiceprints/{vid}/rename")
    def rename_voiceprint(vid: str, req: VpRenameReq):
        e = server.voiceprints.rename(vid, (req.name or "").strip())
        if not e:
            return JSONResponse({"ok": False, "error": "声纹不存在"}, 404)
        server.broadcast_sync({"type": "voiceprints",
                               "voiceprints": server.voiceprints.public()})
        return {"ok": True}

    class VpUpdateReq(BaseModel):
        name: str | None = None
        color: str | None = None
        role: str | None = None

    @app.post("/api/voiceprints/{vid}/update")
    def update_voiceprint(vid: str, req: VpUpdateReq):
        """更新声纹库人员姓名 / 颜色 / 角色"""
        e = server.voiceprints.update(vid, name=req.name, color=req.color, role=req.role)
        if not e:
            return JSONResponse({"ok": False, "error": "声纹不存在"}, 404)
        server.broadcast_sync({"type": "voiceprints",
                               "voiceprints": server.voiceprints.public()})
        return {"ok": True}

    @app.delete("/api/voiceprints/{vid}")
    def delete_voiceprint(vid: str):
        if not server.voiceprints.delete(vid):
            return JSONResponse({"ok": False, "error": "声纹不存在"}, 404)
        server.broadcast_sync({"type": "voiceprints",
                               "voiceprints": server.voiceprints.public()})
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        q = asyncio.Queue(maxsize=500)
        server.clients.add(q)
        await websocket.send_json({"type": "hello", "state": server.state()})
        try:
            while True:
                msg = await q.get()
                await websocket.send_json(msg)
        except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
            pass
        finally:
            server.clients.discard(q)

    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")
    return app


def main():
    global SERVER
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()
    import os
    port = args.port or int(os.environ.get("PORT", "7100"))

    SERVER = Server()
    server = SERVER

    # WAV 全程录音：包装 pipeline.feed
    wav_lock = threading.Lock()
    wav_state = {"f": None}
    orig_feed = server.pipeline.feed

    def feed_with_wav(chunk):
        with wav_lock:
            f = wav_state["f"]
            if f is not None and server.meeting and server.meeting["status"] == "recording":
                pcm = np.clip(chunk, -1, 1)
                f.writeframes((pcm * 32767).astype(np.int16).tobytes())
        orig_feed(chunk)

    server.pipeline.feed = feed_with_wav

    orig_start = server.start_meeting

    def start_and_open_wav(*a, **kw):
        m = orig_start(*a, **kw)
        p = DATA_DIR / m["id"] / "audio.wav"
        f = wave.open(str(p), "wb")
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(SAMPLE_RATE)
        with wav_lock:
            wav_state["f"] = f
        return m

    server.start_meeting = start_and_open_wav

    orig_end = server.end_meeting

    def end_and_close_wav():
        m = orig_end()
        with wav_lock:
            if wav_state["f"] is not None:
                wav_state["f"].close()
                wav_state["f"] = None
        server.maybe_auto_rediarize(m["id"])  # WAV 已落盘，后台自动重分离
        return m

    server.end_meeting = end_and_close_wav

    # 后台加载模型
    def load_models():
        try:
            server.models.load()
            print("[models] loaded", flush=True)
        except Exception as e:
            print(f"[models] load failed: {e}", flush=True)
        server.broadcast_sync({"type": "status", "state": server.state()})

    threading.Thread(target=load_models, daemon=True).start()

    import uvicorn
    app = create_app(server)

    class ServerWithLoop(uvicorn.Server):
        def install_signal_handlers(self):
            pass  # 允许在非主线程运行

        async def startup(self, sockets=None):
            server.loop = asyncio.get_running_loop()
            await super().startup(sockets)

    config = uvicorn.Config(app, host=args.host, port=port, log_level="warning")
    uv = ServerWithLoop(config)
    print(f"会议实时转写系统已启动: http://localhost:{port}/", flush=True)
    uv.run()


if __name__ == "__main__":
    main()
