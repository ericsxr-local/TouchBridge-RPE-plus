"""
TouchBridge -> RPE 后端
-----------------------
Flask + SocketIO 提供可视化界面所需的全部接口：

  - 音频：上传后由【浏览器】播放（HTML5 audio），后端只负责把文件喂给浏览器，
           并接收浏览器持续上报的播放时间，作为全局音乐时钟（精准、稳定、有声音）。
  - OSC  ：纯 Python UDP 监听（不依赖任何第三方库），按 TouchBridge 真实协议解析
           /down /move /hold /drag /tap /up /count，并用 GestureTracker 还原手势。
  - 手势  ：GestureTracker 基于 /down /move /up 自己分类（含死区 / Tap / Hold /
           Flick 红键 / Drag），Flick 必须自己算（TouchBridge 不产生 Flick）。
  - 录制 ：录制时把还原出的"完成的音符"收集起来。
  - 导出 ：events_to_rpe -> RPE / Phigros JSON。

TouchBridge 真实 OSC 协议（来自 TouchOSC/main.swift）：
  /down  [i slot, f x, f y, f area, i zone]      手指落下
  /move  [i slot, f x, f y, f area]              按住中持续上报(~83Hz)
  /hold  [i slot, f x, f y]                       按住>0.22s 且未滑动
  /drag  [i slot, f x, f y]                        滑动开始(移动>0.02 且 dur>0.12s)
  /tap   [i slot, f x, f y, f dur_ms]              快速轻点(dur<0.2s 且移动<0.02)
  /up    [i slot, f dur_ms, f maxDist]             其它抬起(非 tap)
  /count [i count]                                 当前按下的手指数
  坐标 x,y ∈ [0,1]。每条消息都带连续浮点坐标，"9 区域"只是给 Max 选音色。

注意：本程序监听端口与 TouchBridge 发送端口一致（默认 9000）。若端口被旧实例占用，
启动会失败并明确提示。
"""

import os
import json
import time
import math
import struct
import random
import zipfile
import zlib
import tempfile
import socket as pysock
import threading
import socketserver
import webbrowser

from flask import Flask, request, send_from_directory, jsonify
from flask_socketio import SocketIO, emit
from werkzeug.utils import secure_filename

from engine import BeatClock, convert_notes, build_rpe, simulate_session, GestureTracker

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
UPLOAD_DIR = os.path.join(BASE, "uploads")
EXPORT_DIR = os.path.join(BASE, "exports")
EVENT_LOG = "/tmp/touchbridge_events.log"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)

# 允许的封面图片扩展名（同时作为 Phira/RPE 内部的格式标识）
COVER_EXT_MAP = {".png": "png", ".jpg": "jpg", ".jpeg": "jpg",
                 ".gif": "gif", ".webp": "webp", ".bmp": "bmp"}

app = Flask(__name__)
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")


def audit(event, **fields):
    """Small persistent lifecycle log for diagnosing real recording sessions."""
    try:
        row = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, **fields}
        with open(EVENT_LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 全局状态
# ---------------------------------------------------------------------------
class State:
    def __init__(self):
        self.config = self._load_config()
        self.clock = self._build_clock()
        self.notes = []                 # 收集到的"完成的音符"
        self.notes_revision = 0
        self.preview_cache_revision = -1
        self.preview_cache_notes = []
        self.recording = False
        self.audio_filename = None
        # 重启后从 config 恢复音频/曲绘（否则直接导出的 pez 会缺音乐）
        _song = (self.config.get("meta") or {}).get("song")
        if _song and os.path.isfile(os.path.join(UPLOAD_DIR, _song)):
            self.audio_filename = _song
        self.cover_filename = (self.config.get("meta") or {}).get("cover")
        self.last_audio_time = 0.0      # 浏览器上报的播放时间(秒)
        self.last_audio_ts = None       # 上报时刻(单调时钟)
        self.audio_playing = False
        self.manual_start = time.monotonic()
        self.osc_server = None
        self.monitor_clients = set()
        self.tracker = None
        self.monitor_status = "idle"    # idle / listening / failed
        self.self_test_ok = False
        self.osc_port = int(self.config["osc"].get("port", 9000))
        self.last_osc_at = 0.0
        self.osc_packets = 0
        self.last_osc_state_emit = 0.0
        self.native_frames = 0
        self.native_contacts = 0
        self.native_slots = 0
        self.last_raw_move_emit = 0.0

    def _load_config(self):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_config(self):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(self.config, f, ensure_ascii=False, indent=2)

    def _build_clock(self):
        s = self.config["settings"]
        segs = s.get("segments") or None
        return BeatClock(
            bpm=s.get("bpm", 120),
            offset_ms=s.get("offset_ms", 0),
            beats_per_bar=s.get("beats_per_bar", 4),
            segments=segs,
        )

    def rebuild_clock(self):
        self.clock = self._build_clock()

    def current_music_time(self):
        """全局音乐时间(秒)。优先用浏览器上报的音频时钟；否则用内部单调时钟回退。"""
        if self.audio_filename and self.last_audio_ts is not None:
            if self.audio_playing:
                elapsed = time.monotonic() - self.last_audio_ts
                return self.last_audio_time + elapsed
            return self.last_audio_time
        return time.monotonic() - self.manual_start

    def public_state(self):
        s = self.config["settings"]
        return {
            "settings": s,
            "meta": self.config["meta"],
            "osc": self.config["osc"],
            "recording": self.recording,
            "audio_loaded": bool(self.audio_filename),
            "audio_filename": self.audio_filename,
            "cover_loaded": bool(self.cover_filename),
            "cover_filename": self.cover_filename,
            "audio_time": round(self.current_music_time(), 3),
            "note_count": len(self.notes),
            "osc_listening": self.osc_server is not None,
            "monitor_status": self.monitor_status,
            "osc_port": self.osc_port,
            "self_test_ok": self.self_test_ok,
            "collector_alive": bool(self.last_osc_at and time.monotonic() - self.last_osc_at < 2.5),
            "osc_packets": self.osc_packets,
            "native_frames": self.native_frames,
            "native_contacts": self.native_contacts,
            "native_slots": self.native_slots,
        }


STATE = State()


# ---------------------------------------------------------------------------
# 纯 Python OSC 解析（无第三方依赖）
# ---------------------------------------------------------------------------
def _pad4(i):
    return (i + 3) & ~3


def parse_osc_message(data, start=0):
    """解析单条 OSC 消息，返回 (address, args) 或 None。"""
    i = start
    n = len(data)
    end = data.find(b"\x00", i)
    if end < 0:
        return None
    address = data[i:end].decode("utf-8", "replace")
    i = _pad4(end + 1)
    if i >= n or data[i:i + 1] != b",":
        return (address, [])
    end2 = data.find(b"\x00", i)
    tags = data[i + 1:end2].decode("ascii", "replace")
    i = _pad4(end2 + 1)
    args = []
    for t in tags:
        if t == "i":
            if i + 4 > n:
                break
            args.append(int.from_bytes(data[i:i + 4], "big", signed=True))
            i += 4
        elif t == "f":
            if i + 4 > n:
                break
            args.append(struct.unpack(">f", data[i:i + 4])[0])
            i += 4
        elif t == "s" or t == "S":
            e = data.find(b"\x00", i)
            if e < 0:
                break
            args.append(data[i:e].decode("utf-8", "replace"))
            i = _pad4(e + 1)
        elif t == "d":
            if i + 8 > n:
                break
            args.append(struct.unpack(">d", data[i:i + 8])[0])
            i += 8
        elif t == "h":
            if i + 8 > n:
                break
            args.append(int.from_bytes(data[i:i + 8], "big", signed=True))
            i += 8
        else:
            break
    return (address, args)


def parse_osc_packet(data):
    """解析 OSC 包（支持单条消息与 #bundle）。返回 [(address, args), ...]。"""
    if data[:8] == b"#bundle\x00":
        out = []
        i = 16
        n = len(data)
        while i + 4 <= n:
            size = int.from_bytes(data[i:i + 4], "big")
            i += 4
            if i + size > n:
                break
            msg = parse_osc_message(data, i)
            i += size
            if msg:
                out.append(msg)
        return out
    msg = parse_osc_message(data)
    return [msg] if msg else []


def _build_osc(address, floats):
    """构造一条仅含 float 参数的 OSC 消息（用于自检发包）。"""
    def pad(b):
        r = len(b) % 4
        return b + b"\x00" * (0 if r == 0 else 4 - r)
    packet = pad(address.encode() + b"\x00")
    tags = "," + "f" * len(floats)
    packet += pad(tags.encode() + b"\x00")
    for v in floats:
        packet += struct.pack(">f", v)
    return packet


def osc_dispatch(address, args):
    now = time.monotonic()
    STATE.last_osc_at = now
    STATE.osc_packets += 1
    # Heartbeats refresh the UI without entering the gesture classifier.
    if address == "/heartbeat":
        if len(args) >= 1:
            STATE.native_frames = int(args[0])
        if len(args) >= 2:
            STATE.native_contacts = int(args[1])
        if len(args) >= 3:
            STATE.native_slots = int(args[2])
        if now - STATE.last_osc_state_emit >= 0.8:
            STATE.last_osc_state_emit = now
            socketio.emit("state", STATE.public_state())
        return
    # 自检包：标记收到并展示在 monitor，但不参与分类
    if address == "/__selftest__":
        STATE.self_test_ok = True
        socketio.emit("osc_raw", {"address": address, "args": list(args)})
        return
    # /move may arrive hundreds of times per second. Classify every packet,
    # but only send monitor samples to the browser at 20 Hz. Sending them all
    # over Socket.IO eventually starved the actual recording controls.
    if STATE.monitor_clients:
        if address != "/move" or now - STATE.last_raw_move_emit >= 0.05:
            if address == "/move":
                STATE.last_raw_move_emit = now
            socketio.emit("osc_raw", {"address": address, "args": list(args)})
    if not STATE.recording:
        return
    tr = STATE.tracker
    a = args
    try:
        if address == "/down" and len(a) >= 5:
            tr.on_down(int(a[0]), float(a[1]), float(a[2]), float(a[3]), int(a[4]))
        elif address == "/move" and len(a) >= 4:
            tr.on_move(int(a[0]), float(a[1]), float(a[2]), float(a[3]))
        elif address == "/hold" and len(a) >= 3:
            tr.on_hold(int(a[0]), float(a[1]), float(a[2]))
        elif address == "/drag" and len(a) >= 3:
            tr.on_drag(int(a[0]), float(a[1]), float(a[2]))
        elif address == "/tap" and len(a) >= 4:
            tr.on_tap(int(a[0]), float(a[1]), float(a[2]), float(a[3]))
        elif address == "/up" and len(a) >= 3:
            tr.on_up(int(a[0]), float(a[1]), float(a[2]))
        # /count 仅用于监视，不需要还原
    except Exception as e:
        socketio.emit("toast", {"msg": f"OSC 解析异常: {e}", "level": "error"})
    # 取走已完成的音符
    if tr.completed:
        STATE.notes.extend(tr.completed)
        STATE.notes_revision += 1
        tr.completed = []
        socketio.emit("state", STATE.public_state())


class _OSCHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            data = self.request[0]
            for address, args in parse_osc_packet(data):
                osc_dispatch(address, args)
        except Exception:
            pass


class OSCUDPServer(socketserver.UDPServer):
    allow_reuse_address = True


def start_osc():
    if STATE.osc_server:
        return
    port = STATE.osc_port
    try:
        server = OSCUDPServer(("127.0.0.1", port), _OSCHandler)
    except Exception as e:
        STATE.monitor_status = "failed"
        socketio.emit("toast", {"msg": f"OSC 监听失败(端口 {port} 被占用?): {e}", "level": "error"})
        socketio.emit("state", STATE.public_state())
        return
    STATE.osc_server = server
    STATE.monitor_status = "listening"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # loopback 自检：自己发一条测试包，确认监听器能收到并解析
    STATE.self_test_ok = False
    try:
        s = pysock.socket(pysock.AF_INET, pysock.SOCK_DGRAM)
        s.sendto(_build_osc("/__selftest__", [1.0]), ("127.0.0.1", port))
        s.close()
    except Exception:
        pass
    time.sleep(0.25)
    if STATE.self_test_ok:
        socketio.emit("toast", {"msg": f"OSC 监听已启动 @127.0.0.1:{port}（自检通过）", "level": "success"})
    else:
        socketio.emit("toast", {"msg": f"OSC 监听已启动 @127.0.0.1:{port}，但自检未收到回环包（可能环回被禁）", "level": "warn"})
    socketio.emit("state", STATE.public_state())


def stop_osc():
    if STATE.osc_server:
        try:
            STATE.osc_server.shutdown()
            STATE.osc_server.server_close()
        except Exception:
            pass
        STATE.osc_server = None
    STATE.monitor_status = "idle"
    socketio.emit("toast", {"msg": "OSC 监听已停止", "level": "info"})
    socketio.emit("state", STATE.public_state())


@socketio.on("osc_selftest")
def on_osc_selftest():
    if not STATE.osc_server:
        emit("toast", {"msg": "请先点「开始监听 OSC」再自检", "level": "error"})
        return
    STATE.self_test_ok = False
    port = STATE.osc_port
    try:
        s = pysock.socket(pysock.AF_INET, pysock.SOCK_DGRAM)
        s.sendto(_build_osc("/__selftest__", [1.0]), ("127.0.0.1", port))
        s.close()
    except Exception as e:
        emit("toast", {"msg": f"自检发包失败: {e}", "level": "error"})
        return
    time.sleep(0.25)
    if STATE.self_test_ok:
        emit("toast", {"msg": "OSC 自检通过：监听器正常，TouchBridge 的包也能收到", "level": "success"})
    else:
        emit("toast", {"msg": "自检未收到回环包：环回(loopback)可能被系统限制，请检查防火墙", "level": "error"})
    emit("state", STATE.public_state())


# ---------------------------------------------------------------------------
# Socket 事件
# ---------------------------------------------------------------------------
@socketio.on("connect")
def on_connect():
    emit("state", STATE.public_state())


@socketio.on("get_state")
def on_get_state():
    emit("state", STATE.public_state())


@socketio.on("save_settings")
def on_save_settings(data):
    if "settings" in data:
        for k, v in data["settings"].items():
            # OSC port belongs to the transport configuration, not to the
            # gesture settings.  Older frontends sent it in this object, so
            # keep accepting that shape while applying it to the live server.
            if k == "osc_port":
                try:
                    port = int(v)
                    if not 1 <= port <= 65535:
                        raise ValueError
                except (TypeError, ValueError):
                    emit("toast", {"msg": "OSC 端口必须是 1～65535", "level": "error"})
                    continue

                if port != STATE.osc_port:
                    was_listening = STATE.osc_server is not None
                    if was_listening:
                        stop_osc()
                    STATE.osc_port = port
                    STATE.config["osc"]["port"] = port
                    if was_listening:
                        start_osc()
            else:
                STATE.config["settings"][k] = v
    if "meta" in data:
        STATE.config["meta"].update(data["meta"])
    STATE.save_config()
    STATE.rebuild_clock()
    STATE.notes_revision += 1
    emit("state", STATE.public_state())
    emit("toast", {"msg": "设置已保存", "level": "success"})


@socketio.on("audio_loaded")
def on_audio_loaded(data):
    fn = data.get("filename")
    if fn:
        STATE.audio_filename = fn
        STATE.config["meta"]["song"] = fn
        STATE.save_config()
    STATE.last_audio_time = 0.0
    STATE.last_audio_ts = None
    STATE.audio_playing = False
    emit("state", STATE.public_state())
    emit("toast", {"msg": f"音频已就绪：{fn}（用页面上的播放/暂停控制，声音在浏览器播放）", "level": "success"})


@socketio.on("audio_time")
def on_audio_time(data):
    try:
        STATE.last_audio_time = float(data.get("t", 0))
        STATE.audio_playing = bool(data.get("playing", False))
        STATE.last_audio_ts = time.monotonic()
    except Exception:
        pass


@socketio.on("record_start")
def on_record_start(data=None):
    # Recording without an OSC listener silently produces an empty chart.
    # Start it automatically so the monitor button is optional.
    if STATE.osc_server is None:
        start_osc()
    if STATE.osc_server is None:
        emit("toast", {"msg": "无法开始录制：OSC 监听器启动失败", "level": "error"})
        return
    STATE.recording = True
    STATE.tracker = GestureTracker(STATE.current_music_time, STATE.config["settings"])
    audit("record_start", sid=request.sid, notes=len(STATE.notes), osc_packets=STATE.osc_packets,
          native_frames=STATE.native_frames)
    emit("state", STATE.public_state())
    emit("toast", {"msg": "录制中：跟着音乐打板；点「停止录制」结束", "level": "success"})


@socketio.on("record_stop")
def on_record_stop(data=None):
    STATE.recording = False
    if STATE.tracker:
        STATE.tracker.flush_open()
        if STATE.tracker.completed:
            STATE.notes.extend(STATE.tracker.completed)
            STATE.notes_revision += 1
            STATE.tracker.completed = []
    STATE.tracker = None
    reason = data.get("reason", "unknown") if isinstance(data, dict) else "unknown"
    audit("record_stop", sid=request.sid, reason=reason, notes=len(STATE.notes),
          osc_packets=STATE.osc_packets, native_frames=STATE.native_frames)
    emit("state", STATE.public_state())
    emit("toast", {"msg": f"录制停止，共 {len(STATE.notes)} 个音符", "level": "info"})


@socketio.on("clear_events")
def on_clear_events():
    STATE.notes = []
    STATE.notes_revision += 1
    emit("state", STATE.public_state())
    emit("toast", {"msg": "已清空音符", "level": "info"})


@socketio.on("cover_clear")
def on_cover_clear():
    STATE.cover_filename = None
    if "cover" in STATE.config.get("meta", {}):
        STATE.config["meta"]["cover"] = None
        STATE.save_config()
    emit("state", STATE.public_state())
    emit("toast", {"msg": "已清除曲绘，导出将用渐变占位图", "level": "info"})


@socketio.on("monitor_start")
def on_monitor_start():
    STATE.monitor_clients.add(request.sid)
    start_osc()


@socketio.on("monitor_stop")
def on_monitor_stop():
    STATE.monitor_clients.discard(request.sid)


@socketio.on("disconnect")
def on_disconnect():
    STATE.monitor_clients.discard(request.sid)


@socketio.on("sim_inject")
def on_sim_inject():
    notes, _ = simulate_session(duration_sec=4.0, bpm=STATE.config["settings"]["bpm"],
                                offset_ms=STATE.config["settings"]["offset_ms"],
                                seed=random.randint(1, 9999))
    STATE.notes.extend(notes)
    STATE.notes_revision += 1
    emit("state", STATE.public_state())
    emit("toast", {"msg": f"已注入 {len(notes)} 个模拟音符（含 Tap/Hold/Flick/Drag）", "level": "success"})


@socketio.on("auto_calibrate")
def on_auto_calibrate():
    """根据已录制音符相对最近整拍的偏差，自动估算采样补偿偏移(ms)并保存。

    原理：用户跟拍敲击，命中时刻本应落在整拍上；若录音链路 / 音频输出延迟导致
    系统性偏早(快)或偏晚(慢)，所有音符会相对整拍有一个恒定偏差 b(以拍计)。
    取每个音符命中拍的小数部分(循环到 [-0.5,0.5))，其中位数即偏差 b，
    则补偿偏移 = -b * 每拍秒数。正值会让音符延后，抵消"偏早"。
    （假设用户敲在整拍上；若主要敲在反拍/半拍，请改用手动补偿。）
    """
    notes = STATE.notes
    if not notes:
        emit("toast", {"msg": "还没有录制到音符，先录一段再点自动校准", "level": "info"})
        return
    try:
        bpm = float(STATE.clock.segments[0]["bpm"])
        beat_dur = 60.0 / bpm if bpm > 0 else 0.5
        fracs = []
        for n in notes:
            t = n.get("t_sec")
            if t is None:
                continue
            beat = STATE.clock.time_to_beat(t)
            f = beat - math.floor(beat)
            if f > 0.5:
                f -= 1.0
            fracs.append(f)
        if not fracs:
            emit("toast", {"msg": "没有可用的音符时间戳", "level": "info"})
            return
        fracs.sort()
        bias = fracs[len(fracs) // 2]
        offset_ms = round(-bias * beat_dur * 1000.0, 1)
        STATE.config["settings"]["hit_offset_ms"] = offset_ms
        STATE.save_config()
        STATE.notes_revision += 1
        emit("calibrated", {"hit_offset_ms": offset_ms})
        emit("toast", {"msg": f"已自动校准：采样补偿 = {offset_ms:+.1f} ms（正数=音符延后，用于抵消偏早）", "level": "success"})
    except Exception as e:
        emit("toast", {"msg": f"校准失败: {e}", "level": "error"})


@socketio.on("get_preview")
def on_get_preview(data=None):
    if STATE.preview_cache_revision != STATE.notes_revision:
        STATE.preview_cache_notes = convert_notes(STATE.notes, STATE.clock, STATE.config["settings"])
        STATE.preview_cache_revision = STATE.notes_revision
    playhead = STATE.clock.time_to_beat(STATE.current_music_time())
    payload = {
        "playhead": round(playhead, 4),
        "audioTime": round(STATE.current_music_time(), 4),
        "bpm": STATE.config["settings"]["bpm"],
        "beats_per_bar": STATE.config["settings"]["beats_per_bar"],
        "segments": STATE.clock.segments,
        "offset_ms": STATE.clock.offset_sec * 1000.0,
        "revision": STATE.notes_revision,
    }
    known = data.get("revision", -1) if isinstance(data, dict) else -1
    if known != STATE.notes_revision:
        payload["notes"] = STATE.preview_cache_notes
    emit("preview", payload)


@socketio.on("export_rpe")
def on_export_rpe():
    rpe, stats = build_rpe(STATE.notes, STATE.clock, STATE.config["settings"], STATE.config["meta"])
    name = (STATE.config["meta"].get("name") or "chart").strip().replace(" ", "_") or "chart"

    # 只导出通用 .pez：扁平结构（根目录 info.txt + info.yml + <id>.json/音乐/曲绘），
    # Phira（iPad）与 RPE/PhiEdit 桌面版（Windows）都直接导入这一个文件即可，
    # 无需再单独导出 json / 曲绘文件。
    pez_fname, pez_note = _build_pez(rpe, name)

    if pez_fname:
        msg = f"已导出通用谱包 {pez_fname}：{stats}"
        if pez_note:
            msg += f"（{pez_note}）"
        emit("exported", {"pez": pez_fname, "stats": stats})
        emit("toast", {"msg": msg, "level": "success"})
    else:
        emit("exported", {"pez": None, "stats": stats})
        emit("toast", {"msg": f"导出失败：{stats}", "level": "error"})


def _audio_length_sec(path):
    """尽力读取音频时长（秒）；读不出返回 None。wav 用标准库精确解析。"""
    try:
        if path.lower().endswith(".wav"):
            import wave
            with wave.open(path, "rb") as w:
                return round(w.getnframes() / float(w.getframerate()), 3)
    except Exception:
        pass
    return None


def _is_image(data):
    """按魔数粗略校验图片内容（避免把非图片塞进谱包）。"""
    if data[:8].startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if data[:2] == b"\xff\xd8":          # JPEG
        return True
    if data[:4] == b"GIF8":             # GIF
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    if data[:2] == b"BM":               # BMP
        return True
    return False


def _ensure_illustration_file():
    """准备曲绘文件，返回 (in_name, abs_path)。

    in_name 固定为干净的 ASCII 名 ``background.<ext>``（占位图为 ``background.png``）。
    之所以用 ASCII 固定名而不是 ``<谱名>_cover.png``：Phira 解压 .pez 后按文件名
    加载曲绘，中文/空格文件名在 zip 里极易被读成乱码 → 曲绘加载失败（表现就是
    "曲绘没打进 pez"）。``background.png`` 同时是 Phira 的默认约定文件名，最稳。
    abs_path 是该文件在本机的真实路径。优先使用上传的曲绘；没有则生成渐变占位图。
    """
    if STATE.cover_filename:
        src = os.path.join(UPLOAD_DIR, STATE.cover_filename)
        if os.path.isfile(src):
            ext = os.path.splitext(STATE.cover_filename)[1].lower()
            ext = COVER_EXT_MAP.get(ext, "png")
            return f"background.{ext}", src
    # 生成渐变占位图（写到系统临时目录，避免污染 exports 输出文件夹）
    gen_path = os.path.join(tempfile.gettempdir(), "tb_illustration_cache.png")
    _make_illustration(gen_path)
    return "background.png", gen_path


def _make_illustration(path, size=512):
    """生成一张简洁的渐变占位封面 PNG（纯标准库实现）。"""
    width = height = size
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # PNG 行过滤器：None
        for x in range(width):
            # 深蓝 -> 深紫 对角渐变
            r = int(30 + 60 * (x / width))
            g = int(30 + 20 * (y / height))
            b = int(70 + 80 * ((x + y) / (width + height)))
            raw += bytes((r, g, b))

    def _chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8bit RGB
    png = (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
           + _chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + _chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def _yaml_str(s):
    """字符串安全地写成 YAML 标量（JSON 字符串是合法 YAML）。"""
    return json.dumps(str(s), ensure_ascii=False)


def _build_pez(rpe, name):
    """打一个 Phira（iPad）与 PhiEdit/RPE 桌面版（Windows）都能直导的通用 .pez（zip）。

    官方 PEZ 规范（pgrfm wiki "PEZ" 条目）：PEZ 本质是 zip，内部所有文件
    （谱面 json、音乐、曲绘、info.txt）必须**平铺在压缩包根目录**，
    不能有任何子目录结构。RPE 桌面版读 info.txt（"#\\nName: ...\\nSong: ..."
    的 key: value 文本，UTF-8 + CRLF，字段见用户截图），Phira 读 info.yml。
    两份 info 都放进去、指向同一套 ``<id>.*`` 文件（完全模仿 RPE 自己导出时
    的命名），两个平台各取所需、互不干扰。
    """
    meta = STATE.config["meta"]
    try:
        pid = str(time.time_ns() // 1000)              # 16 位数字 id，仿 RPE 原生
        pid = pid[-16:] if len(pid) > 16 else pid.zfill(16)

        in_name, illus_src = _ensure_illustration_file()
        illus_ext = os.path.splitext(in_name)[1]       # .png/.jpg/...
        a_ext = os.path.splitext(STATE.audio_filename)[1].lower() if STATE.audio_filename else ""

        # 包内统一用 <id>.* 命名（RPE 原生导出即如此），谱面 META 引用同步改写保持自洽
        proj = json.loads(json.dumps(rpe, ensure_ascii=False))
        proj["META"]["illustration"] = pid + illus_ext
        proj["META"]["background"] = pid + illus_ext
        if STATE.audio_filename:
            proj["META"]["song"] = pid + a_ext

        # 音乐与时长
        music_src = None
        length = None
        if STATE.audio_filename:
            cand = os.path.join(UPLOAD_DIR, STATE.audio_filename)
            if os.path.isfile(cand):
                music_src = cand
                length = _audio_length_sec(cand)

        # --- info.txt（RPE/PhiEdit 桌面版专用，UTF-8 + CRLF，首行 "#"）---
        now = time.localtime()
        last_edit = "_".join(str(now[i]) for i in (0, 1, 2, 3, 4, 5)) + "_"
        try:
            level_val = int(str(meta.get("level") or "").strip())
        except ValueError:
            level_val = 0
        lines = [
            "#",
            f"Name: {meta.get('name') or name}",
            f"Path: {pid}",
            f"Song: {pid}{a_ext}",
            f"Picture: {pid}{illus_ext}",
            f"Chart: {pid}.json",
            f"Level: {level_val}",
            f"Composer: {meta.get('composer') or ''}",
            f"Charter: {meta.get('charter') or 'TouchBridge'}",
            f"LastEditTime: {last_edit}",
        ]
        if length is not None:
            lines.append(f"Length: {length}")
        lines.append("EditTime: 0.0")
        lines.append("Group: Default")

        # --- info.yml（Phira 专用，字段与 info.txt 指向同一套文件）---
        info_fields = {
            "name": meta.get("name") or name,
            "difficulty": 0,
            "level": str(meta.get("level") or "SP"),
            "charter": meta.get("charter") or "TouchBridge",
            "composer": meta.get("composer") or "",
            "illustrator": "",
            "chart": f"{pid}.json",
            "format": "rpe",
            "music": (pid + a_ext) if music_src else "",
            "illustration": pid + illus_ext,
        }
        yml = "\n".join(f"{k}: {_yaml_str(v) if isinstance(v, str) else v}" for k, v in info_fields.items())

        pez_path = os.path.join(EXPORT_DIR, f"{name}.pez")
        with zipfile.ZipFile(pez_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("info.txt", "\r\n".join(lines) + "\r\n")
            z.writestr("info.yml", yml + "\n")
            z.writestr(f"{pid}.json", json.dumps(proj, ensure_ascii=False, indent=2))
            z.write(illus_src, pid + illus_ext)
            if music_src:
                z.write(music_src, pid + a_ext)
        note = None
        if not music_src:
            note = "未导入音频，包内暂无音乐文件"
        return os.path.basename(pez_path), note
    except Exception as e:  # 打包失败不阻塞 json 导出
        return None, f"pez 打包失败: {e}"


# ---------------------------------------------------------------------------
# HTTP 路由
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    response = send_from_directory(os.path.join(BASE, "..", "frontend"), "index.html")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-TouchBridge-Edition"] = "Eric_SXR"
    response.headers["X-TouchBridge-Origin"] = "upstream-and-community-derived"
    return response


@app.route("/health")
def health():
    """Local diagnostics for the launcher and support checks."""
    data = STATE.public_state()
    data["edition"] = "Eric_SXR"
    data["original_work"] = False
    return jsonify(data)


@app.route("/vendor/<path:filename>")
def vendor(filename):
    return send_from_directory(os.path.join(BASE, "..", "frontend"), filename)


@app.route("/audio/<path:filename>")
def serve_audio(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"ok": False, "msg": "no file"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "msg": "empty filename"}), 400
    # 净化文件名，防止路径穿越/特殊字符（服务仅监听 127.0.0.1，加固属于防御性编程）
    fn = secure_filename(f.filename) or "audio.bin"
    f.save(os.path.join(UPLOAD_DIR, fn))
    return jsonify({"ok": True, "filename": fn})


@app.route("/upload_cover", methods=["POST"])
def upload_cover():
    if "file" not in request.files:
        return jsonify({"ok": False, "msg": "no file"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "msg": "empty filename"}), 400
    data = f.read()
    ext = os.path.splitext(secure_filename(f.filename))[1].lower()
    if ext not in COVER_EXT_MAP:
        return jsonify({"ok": False, "msg": "请上传图片文件（png/jpg/gif/webp/bmp）"}), 400
    if len(data) > 10 * 1024 * 1024:
        return jsonify({"ok": False, "msg": "图片过大（上限 10MB）"}), 400
    if not _is_image(data):
        return jsonify({"ok": False, "msg": "文件内容不是有效的图片"}), 400
    # 用稳定的文件名，避免中文/空格在跨平台传输时出问题
    safe = "cover_" + str(int(time.time())) + ext
    with open(os.path.join(UPLOAD_DIR, safe), "wb") as out:
        out.write(data)
    STATE.cover_filename = safe
    STATE.config.setdefault("meta", {})["cover"] = safe
    STATE.save_config()
    return jsonify({"ok": True, "filename": safe})


@app.route("/download/<filename>")
def download(filename):
    return send_from_directory(EXPORT_DIR, filename, as_attachment=True)


@app.route("/open_exports")
def open_exports():
    """在文件管理器中打开导出目录（解决"文件放哪了"的问题）。"""
    import subprocess
    import platform
    try:
        sys_name = platform.system()
        if sys_name == "Darwin":
            subprocess.Popen(["open", EXPORT_DIR])
        elif sys_name == "Windows":
            subprocess.Popen(["explorer", EXPORT_DIR])
        else:
            subprocess.Popen(["xdg-open", EXPORT_DIR])
        return jsonify({"ok": True, "dir": EXPORT_DIR})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


if __name__ == "__main__":
    WEB_PORT = int(os.environ.get("WEB_PORT", 8765))
    url = f"http://127.0.0.1:{WEB_PORT}"
    print(f"TouchBridge->RPE 运行中：{url}")
    # Keep one UDP socket for the backend lifetime. Stop/Export must not make
    # a later recording session silently lose input.
    start_osc()
    if os.environ.get("TOUCHBRIDGE_NO_BROWSER") != "1":
        try:
            webbrowser.open(url)
        except Exception:
            pass
    socketio.run(app, host="127.0.0.1", port=WEB_PORT, debug=False, allow_unsafe_werkzeug=True)
