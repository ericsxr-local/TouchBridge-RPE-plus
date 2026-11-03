"""
TouchBridge -> RPE 核心引擎（纯 Python，无第三方依赖，可单元测试）

职责：
  1. BeatClock  : 音乐播放时间(秒) <-> 绝对拍数，支持 offset 与多段变速(BPMList)
  2. quantize   : 把连续拍数吸附到量化网格，带有"吸附容差"容错区间
  3. map_x      : 把触控板 X(0~1) 或 9 区域编号 -> RPE 的 positionX(-675~675)
  4. convert_notes / build_rpe : 把"完成的音符"转成 RPE / Phigros 的 JSON 谱面

音符模型（本程序内部）：
  {'gesture':'tap'|'hold'|'flick'|'drag', 'finger':int, 't_sec':float(秒), 'x':float(0~1),
   'hold_sec':float(可选, hold 时长), 'path':[(t_sec,x,y),...](可选, drag 轨迹)}

RPE 音符类型（Phigros Wiki 确认）：
  type 1 = Tap(蓝键)   2 = Hold(长条)   3 = Flick(红键/粉键)   4 = Drag(黄键)
"""

import math
import random
from fractions import Fraction

# ---------------------------------------------------------------------------
# 手势类型常量
# ---------------------------------------------------------------------------
TAP, HOLD, FLICK, DRAG = "tap", "hold", "flick", "drag"
NOTE_TYPE = {"tap": 1, "hold": 2, "flick": 3, "drag": 4}


# ---------------------------------------------------------------------------
# 手势分类器（基于 /down /move /up 自分类，含死区 / Flick 红键）
# 不依赖 Flask，可单元测试。
# ---------------------------------------------------------------------------
class GestureTracker:
    def __init__(self, get_time, settings):
        self.get_time = get_time
        self.settings = settings
        self.slots = {}        # slot -> 进行中的手势
        self.completed = []    # 已完成的音符（待取走）
        self._gid = 0          # 手势分组 id（用于预览时把同一段拖拽连成一条线）

    def _th(self, key, default):
        try:
            v = self.settings.get(key)
            return float(v) if v is not None else default
        except Exception:
            return default

    def deadZone(self):     return self._th("deadZone", 0.03)
    def holdMinTime(self):  return self._th("holdMinTime", 0.2)
    def flickWindow(self):  return self._th("flickWindow", 0.2)
    def flickVel(self):     return self._th("flick_speed", 2.0)
    def flickMaxDist(self): return self._th("flickMaxDist", 0.25)
    def dragMinSpeed(self): return self._th("dragMinSpeed", 0.25)  # 平均移动速度低于此值 -> 不算拖拽(归为 Hold)
    def dragMaxDur(self):   return self._th("dragMaxDur", 1.5)     # 移动时长超过此值 -> 归为 Hold(避免超长拖尾)

    def _finalize(self, slot, gesture, **kw):
        kw["gesture"] = gesture
        kw["finger"] = slot
        kw["gid"] = self._gid
        self._gid += 1
        self.completed.append(kw)
        self.slots.pop(slot, None)

    def _classify(self, s, dur, max_dist):
        """根据已收集的运动特征判定最终手势（死区分支已在调用处处理）。"""
        t0 = s["down_t"]
        sx, sy = s["sx"], s["sy"]
        peak_vel = s["peak_vel"]
        last = s["traj"][-1] if s["traj"] else (t0, sx, sy)
        lx, ly = last[1], last[2]
        # 1) 快速短划 -> Flick 红键（命中时刻取按下起点，最贴近真实打击点）
        if (dur <= self.flickWindow()) and (peak_vel >= self.flickVel()) and (max_dist <= self.flickMaxDist()):
            dx, dy = lx - sx, ly - sy
            nrm = math.hypot(dx, dy) or 1e-6
            return ("flick", {"t_sec": t0, "x": lx, "dir": [dx / nrm, dy / nrm]})
        # 2) 慢速漂移 / 超长移动 -> 归为 Hold（手指长按时轻微移动，不应变成拖拽长尾）
        avg_speed = max_dist / dur if dur > 1e-4 else 0.0
        if avg_speed < self.dragMinSpeed() or dur > self.dragMaxDur():
            return ("hold", {"t_sec": t0, "x": sx, "hold_sec": dur})
        # 3) 否则 -> Drag（保留轨迹做平滑曲线）
        return ("drag", {"t_sec": t0, "x": sx, "path": list(s["traj"])})

    def on_down(self, slot, x, y, area, zone):
        t = self.get_time()
        self.slots[slot] = {
            "down_t": t, "sx": x, "sy": y, "last_t": t, "last_x": x, "last_y": y,
            "peak_vel": 0.0, "max_dist": 0.0, "traj": [(t, x, y)], "dragging": False,
        }

    def on_move(self, slot, x, y, area):
        s = self.slots.get(slot)
        if not s:
            return
        t = self.get_time()
        dt = t - s["last_t"]
        dx = x - s["last_x"]
        dy = y - s["last_y"]
        seg = math.hypot(dx, dy)
        if dt > 1e-4:
            vel = seg / dt
            s["peak_vel"] = max(s["peak_vel"], vel)
        s["max_dist"] = max(s["max_dist"], math.hypot(x - s["sx"], y - s["sy"]))
        s["last_t"] = t
        s["last_x"] = x
        s["last_y"] = y
        s["traj"].append((t, x, y))

    def on_hold(self, slot, x, y):
        s = self.slots.get(slot)
        if s:
            s["dragging"] = True

    def on_drag(self, slot, x, y):
        s = self.slots.get(slot)
        if s:
            s["dragging"] = True

    def on_tap(self, slot, x, y, dur_ms):
        # 命中时刻取"手指落下"起点（而非抬起），避免系统性晚 100ms+
        s = self.slots.get(slot)
        if s:
            self._finalize(slot, "tap", t_sec=s["down_t"], x=s["sx"])
        else:
            self._finalize(slot, "tap", t_sec=self.get_time(), x=x)

    def on_up(self, slot, dur_ms, maxDist):
        s = self.slots.get(slot)
        if not s:
            return
        dur = dur_ms / 1000.0
        max_dist = max(s["max_dist"], float(maxDist))
        if max_dist < self.deadZone():
            if dur >= self.holdMinTime():
                self._finalize(slot, "hold", t_sec=s["down_t"], x=s["sx"], hold_sec=dur)
            else:
                self._finalize(slot, "tap", t_sec=s["down_t"], x=s["sx"])
        else:
            g, kw = self._classify(s, dur, max_dist)
            self._finalize(slot, g, **kw)

    def flush_open(self):
        now = self.get_time()
        for slot, s in list(self.slots.items()):
            dur = now - s["down_t"]
            max_dist = s["max_dist"]
            if max_dist < self.deadZone():
                if dur >= self.holdMinTime():
                    self._finalize(slot, "hold", t_sec=s["down_t"], x=s["sx"], hold_sec=dur)
                else:
                    self._finalize(slot, "tap", t_sec=s["down_t"], x=s["sx"])
            else:
                g, kw = self._classify(s, dur, max_dist)
                self._finalize(slot, g, **kw)
        self.slots.clear()


# ---------------------------------------------------------------------------
# 1. 拍时钟：处理 offset 与变速
# ---------------------------------------------------------------------------
class BeatClock:
    def __init__(self, bpm=120.0, offset_ms=0.0, beats_per_bar=4, segments=None):
        """
        segments: list[{'startBeat': float, 'bpm': float}, ...]
                  按 startBeat 升序；第一段通常 startBeat=0。
                  为空时退化为单一 BPM。
        """
        self.beats_per_bar = int(beats_per_bar)
        self.offset_sec = offset_ms / 1000.0
        segs = sorted(segments, key=lambda s: s["startBeat"]) if segments else [{"startBeat": 0.0, "bpm": float(bpm)}]
        if segs[0]["startBeat"] != 0.0:
            segs = [{"startBeat": 0.0, "bpm": segs[0]["bpm"]}] + segs
        self.segments = segs
        self._times = []
        for i, s in enumerate(segs):
            if i == 0:
                self._times.append(0.0)
            else:
                prev = segs[i - 1]
                dt = (s["startBeat"] - prev["startBeat"]) * 60.0 / prev["bpm"]
                self._times.append(self._times[i - 1] + dt)

    def time_to_beat(self, music_time):
        ct = music_time - self.offset_sec
        for i in range(len(self.segments) - 1, -1, -1):
            if ct >= self._times[i] or i == 0:
                s = self.segments[i]
                return s["startBeat"] + (ct - self._times[i]) * s["bpm"] / 60.0
        return 0.0

    def beat_to_time(self, beat):
        for i in range(len(self.segments) - 1, -1, -1):
            if beat >= self.segments[i]["startBeat"] or i == 0:
                s = self.segments[i]
                ct = self._times[i] + (beat - s["startBeat"]) * 60.0 / s["bpm"]
                return ct + self.offset_sec
        return 0.0

    def beat_to_bbt(self, beat):
        """绝对拍数 -> RPE Beat [整数拍, 分子, 分母]（全整数）。

        RPE 规定：节拍值 = Beat[0] + Beat[1] / Beat[2]（docs.lchzh.net 及 cmdysj 官方
        rpe-next 解析器 beatValue 均如此实现）。**Beat[0] 是拍数的整数部分，不是小节！**
        不要乘以每小节拍数——否则整个谱面音符会被压缩数倍、堆叠成一团。
        例：3.5 拍 -> [3, 1, 2]；6.0 拍 -> [6, 0, 1]。
        第三位必须是正整数分母，写成 0 或小数都会让 RPE 解析出 0/0=NaN。
        """
        f = Fraction(float(beat)).limit_denominator(1000)
        if f < 0:
            f = Fraction(0)
        whole = int(f)                      # 整数拍（非小节）
        rem = f - whole
        return [whole, int(rem.numerator), int(rem.denominator)]


# ---------------------------------------------------------------------------
# 2. 量化（网格 + 吸附容差）
# ---------------------------------------------------------------------------
def quantize(beat, subdivision=8, tolerance=0.6):
    """
    subdivision: 每拍细分（4=1/4拍, 8=1/8拍, 16=1/16拍）
    tolerance  : 0~1，吸附容差。0=从不吸附(保留自由位置)，1=始终吸附。
    只有 |beat - 最近网格| <= tolerance * grid 才吸附，否则保留自由拍位。
    """
    grid = 1.0 / max(1, int(subdivision))
    nearest = round(beat / grid) * grid
    if abs(beat - nearest) <= tolerance * grid:
        return nearest
    return beat


# ---------------------------------------------------------------------------
# 3. 坐标映射
# ---------------------------------------------------------------------------
def map_x(x, x_mode="coordinate", regions=9):
    """
    x_mode='coordinate' : x 为 0~1 的连续比例 -> positionX(-675~675)
    x_mode='region'     : x 为 0~regions-1 的区域编号 -> 取区域中心
    """
    if x_mode == "region":
        idx = int(round(float(x)))
        idx = max(0, min(regions - 1, idx))
        x01 = (idx + 0.5) / regions
    else:
        x01 = max(0.0, min(1.0, float(x)))
    pos = -675.0 + x01 * 1350.0
    return max(-675.0, min(675.0, pos))


# ---------------------------------------------------------------------------
# 4. 音符 -> RPE 音符
# ---------------------------------------------------------------------------
def _make_note(note_type, start_beat, end_beat, pos_x, clock, alpha=255):
    """构造 RPE 兼容音符。

    RPE 音符时间字段是 startTime/endTime（Beat 三整数数组 [小节,分子,分母]），
    不是浮点拍数；type 2(Hold) 的 endTime 为长条结束，其余音符与 startTime 一致。
    startBeat/endBeat 为内部浮点拍（预览用，PhiZone 也能识别）。
    """
    start_beat = round(start_beat, 6)
    end_beat = round(end_beat, 6)
    bbt_s = clock.beat_to_bbt(start_beat)
    bbt_e = clock.beat_to_bbt(end_beat)
    return {
        "type": note_type,
        "startTime": bbt_s,       # RPE: Beat [bar, num, den]
        "endTime": bbt_e,         # RPE: Hold 结束（其它与 startTime 一致）
        "positionX": round(pos_x, 3),
        "above": 1,
        "alpha": alpha,
        "size": 1.0,
        "speed": 1.0,
        "yOffset": 0.0,
        "visibleTime": 999999.0,
        "isFake": 0,
        # 内部字段（预览/统计用；导出时由 build_rpe 保留，RPE 会忽略未知字段）
        "startBeat": start_beat,
        "endBeat": end_beat,
    }


def convert_notes(notes, clock, settings):
    """
    把内部音符列表转成 RPE 音符字典列表（含量化、坐标映射、拖拽下采样）。
    返回的字典同时用于预览与导出。
    """
    subdivision = int(settings.get("subdivision", 8))
    tolerance = float(settings.get("snap_tolerance", 0.6))
    debounce = float(settings.get("debounce_ms", 40)) / 1000.0
    x_mode = settings.get("x_mode", "coordinate")
    regions = int(settings.get("regions", 9))
    drag_sample_ms = float(settings.get("drag_sample_ms", 25))
    # 采样补偿：把录制到的命中时刻整体平移（毫秒→秒）。正值=音符延后，负值=提前。
    # 用于抵消录音链路 / 音频输出延迟导致的系统性"偏早(快)"或"偏晚(慢)"。
    # 由网页"采样补偿(ms)"或「自动校准」设置。
    offs = float(settings.get("hit_offset_ms", 0)) / 1000.0

    out = []
    last_finger_beat = {}  # finger -> 上次量化拍（去抖）

    for n in notes:
        g = n["gesture"]
        fid = n.get("finger", 0)

        if g in (TAP, FLICK):
            b = quantize(clock.time_to_beat(n["t_sec"] + offs), subdivision, tolerance)
            if fid in last_finger_beat and abs(b - last_finger_beat[fid]) < debounce:
                continue
            last_finger_beat[fid] = b
            pos = map_x(n["x"], x_mode, regions)
            note = _make_note(NOTE_TYPE[g], b, b, pos, clock)
            if g == FLICK and "dir" in n:
                note["dir"] = n["dir"]   # 预览画箭头用，RPE 忽略多余字段
            out.append(note)

        elif g == HOLD:
            tb = n["t_sec"] + offs
            s = quantize(clock.time_to_beat(tb), subdivision, tolerance)
            e = quantize(clock.time_to_beat(tb + n.get("hold_sec", 0)), subdivision, tolerance)
            if fid in last_finger_beat and abs(s - last_finger_beat[fid]) < debounce:
                continue
            last_finger_beat[fid] = s
            if e <= s:
                e = s + (1.0 / max(1, subdivision))  # 至少一小格
            pos = map_x(n["x"], x_mode, regions)
            out.append(_make_note(NOTE_TYPE[HOLD], s, e, pos, clock))

        elif g == DRAG:
            path = n.get("path") or []
            gid = n.get("gid")
            if path:
                # 1) 最小点间距过滤：去掉抖动产生的过近点（根治"乱长轨迹"）
                min_sp = 0.012
                pts = [path[0]]
                for p in path[1:]:
                    if math.hypot(p[1] - pts[-1][1], p[2] - pts[-1][2]) >= min_sp:
                        pts.append(p)
                # 2) 滑动平均平滑 x（去抖），保留时间
                if len(pts) >= 3:
                    sm = [pts[0]]
                    for i in range(1, len(pts) - 1):
                        sm.append((pts[i][0], (pts[i - 1][1] + pts[i][1] + pts[i + 1][1]) / 3.0, pts[i][2]))
                    sm.append(pts[-1])
                    pts = sm
                # 3) 按 drag_sample_ms 下采样（时间统一加 offs 补偿）
                sampled = [(pts[0][0] + offs, pts[0][1], pts[0][2])]
                last_t = pts[0][0]
                for p in pts[1:]:
                    if (p[0] - last_t) >= drag_sample_ms / 1000.0:
                        sampled.append((p[0] + offs, p[1], p[2]))
                        last_t = p[0]
            else:
                sampled = [(n["t_sec"] + offs, n["x"], 0.5)]
            # Several 25ms samples often snap onto the same grid beat. Merge
            # those samples into one averaged point so yellow notes form a
            # chronological path instead of a pile at identical timestamps.
            buckets = {}
            for (t, x, y) in sampled:
                b = quantize(clock.time_to_beat(t), subdivision, tolerance)
                bucket = buckets.setdefault(b, [0.0, 0])
                bucket[0] += x
                bucket[1] += 1
            for b, (sum_x, count) in buckets.items():
                pos = map_x(sum_x / count, x_mode, regions)
                note = _make_note(NOTE_TYPE[DRAG], b, b, pos, clock)
                if gid is not None:
                    note["gid"] = gid
                out.append(note)

    # Chronological order is primary. For notes on the exact same beat only,
    # place non-Hold before Hold as required by RPE.
    out.sort(key=lambda n: (n["startBeat"], 1 if n["type"] == 2 else 0, n["positionX"]))
    return out


def build_rpe(notes, clock, settings, meta):
    """把音符列表构建成完整 RPE / Phigros JSON。返回 (rpe_dict, stats_dict)。

    格式依据 Re:PhiEdit 官方规范（docs.lchzh.net/learning/phiediter）：
      - BPMList: [{startTime: Beat, bpm: float}]
      - judgeLineList: 判定线必须带 eventLayers（含 speedEvents 等常驻事件），
        否则部分编辑器无法计算下落速度/网格。
      - 音符时间全部为 Beat 三整数 [小节, 分子, 分母]。
    """
    converted = convert_notes(notes, clock, settings)

    # 导出净化：去掉仅预览使用的内部键（gid/dir），其余字段均为 RPE 合法字段
    export_keys = ("type", "startTime", "endTime", "positionX", "above", "alpha",
                   "size", "speed", "yOffset", "visibleTime", "isFake")
    rpe_notes = [{k: n[k] for k in export_keys if k in n} for n in converted]

    stats = {"tap": 0, "hold": 0, "flick": 0, "drag": 0}
    for n in rpe_notes:
        if n["type"] == 1:
            stats["tap"] += 1
        elif n["type"] == 2:
            stats["hold"] += 1
        elif n["type"] == 3:
            stats["flick"] += 1
        elif n["type"] == 4:
            stats["drag"] += 1

    # 常驻事件层：速度恒定 10（RPE 官方 createLine 默认；若写成 1 音符流速极慢、
    # 挤在判定线附近显得"扭曲"）、位置/旋转 0、不透明度 255（覆盖整谱）。
    #
    # 两个易踩的坑（都会导致桌面版 PhiEdit 里"连手动放的音符都变形"）：
    #  1. 普通事件（moveX/moveY/rotate/alpha）必须带 easingType（对齐官方 createEvent），
    #     缺失时部分版本插值结果为 NaN → 判定线坐标/透明度每帧算出 NaN，整线渲染错乱；
    #  2. speedEvents 必须带 floorPosition（起始拍的累计位移，桌面版 PhiEdit 读它），
    #     缺失时音符与判定线的相对纵向位置会算错，音符散落/堆叠；
    #     同时按官方规范把 start/end 也写上（Phira(prpr) 读 start/end）。
    _EASING_LINEAR = 1

    # 事件结束拍：覆盖整谱即可（取最后一个音符之后 16 拍，至少 64 拍），
    # 避免早年用 10000 拍在部分编辑器事件视图里显示成离谱数字。
    max_beat = max((n["endBeat"] for n in converted), default=0.0)
    end_beat = max(64.0, max_beat + 16.0)
    end_bbt = clock.beat_to_bbt(end_beat)

    def _const_line_event(start_val, end_val):
        """moveX/moveY/rotate/alpha 常驻事件（对齐官方 createEvent 的完整字段）。"""
        return {
            "startTime": clock.beat_to_bbt(0.0),
            "endTime": end_bbt,
            "start": start_val,
            "end": end_val,
            "easingType": _EASING_LINEAR,
            "easingLeft": 0.0,
            "easingRight": 1.0,
            "bezier": 0,
            "bezierPoints": [0.0, 0.0, 1.0, 1.0],
            "linkgroup": 0,
        }

    def _const_speed_event(value):
        """speedEvents 常驻事件。

        兼容两种读取方：
        - 桌面版 PhiEdit 读 value / floorPosition（SpeedEvent 无缓动字段）；
        - Phira(prpr) 读 start / end（把速度事件当普通事件反序列化，缺了直接报错）。
        两组字段都写上，互不干扰。
        """
        return {
            "startTime": clock.beat_to_bbt(0.0),
            "endTime": end_bbt,
            "value": value,
            "floorPosition": 0.0,
            "start": value,
            "end": value,
        }

    default_layer = {
        "speedEvents": [_const_speed_event(10.0)],
        "moveXEvents": [_const_line_event(0.0, 0.0)],
        "moveYEvents": [_const_line_event(0.0, 0.0)],
        "rotateEvents": [_const_line_event(0.0, 0.0)],
        "alphaEvents": [_const_line_event(255.0, 255.0)],
    }

    judge_line = {
        "Group": 0,
        "Name": "TouchBridge Line",
        "Texture": "line.png",
        "anchor": [0.5, 0.5],
        "bpmfactor": 1.0,
        "rotateWithFather": True,
        "isGif": False,
        "eventLayers": [default_layer],
        "extended": {},
        "father": -1,
        "isCover": 1,
        "numOfNotes": len(rpe_notes),
        "zOrder": 0,
        "notes": rpe_notes,
    }

    # BPM 事件：startTime 为 Beat 三整数；bpm 同时给小写（官方规范）与大写（旧示例）两份键，兼容各版本
    bpm_list = []
    for s in clock.segments:
        bbt = clock.beat_to_bbt(s["startBeat"])
        bpm_list.append({
            "startTime": bbt,
            "bpm": float(s["bpm"]),
            "BPM": float(s["bpm"]),
            "startBeat": float(s["startBeat"]),
            "startTimeSec": round(clock.beat_to_time(s["startBeat"]), 6),
        })

    rpe_id = str(meta.get("id") or random.randint(100000, 999999))
    rpe = {
        "BPMList": bpm_list,
        "META": {
            "RPEVersion": 160,
            "background": meta.get("background", ""),
            "charter": meta.get("charter", "TouchBridge"),
            "composer": meta.get("composer", ""),
            "id": rpe_id,
            "illustration": meta.get("illustration", ""),
            "level": meta.get("level", ""),
            "name": meta.get("name", "Untitled"),
            # Phira(prpr) 把 META.offset 反序列化为 i32，必须写整数，
            # 写 0.0 这类浮点会让 Phira 报 invalid type: floating point。
            "offset": int(round(clock.offset_sec * 1000.0)),
            "song": meta.get("song", ""),
        },
        "judgeLineGroup": ["Default", "", "", "", ""],
        "judgeLineList": [judge_line],
    }
    return rpe, stats


# ---------------------------------------------------------------------------
# 5. 模拟手势（无 TouchBridge 时测试管线）
# ---------------------------------------------------------------------------
def simulate_session(duration_sec=12.0, bpm=120.0, offset_ms=0.0, seed=42):
    """生成一段示例"完成的音符"，用来在没有触控板时验证整条管线。"""
    random.seed(seed)
    clock = BeatClock(bpm=bpm, offset_ms=offset_ms)
    notes = []
    t = 1.0
    finger = 0
    while t < duration_sec:
        beat = clock.time_to_beat(t)
        r = random.random()
        x = random.random()
        if r < 0.55:
            notes.append({"gesture": TAP, "finger": finger % 5, "t_sec": t, "x": x})
            t += 0.25 + random.random() * 0.25
        elif r < 0.78:
            dur = 0.3 + random.random() * 0.5
            notes.append({"gesture": HOLD, "finger": finger % 5, "t_sec": t, "x": x, "hold_sec": dur})
            t += dur + 0.2
        elif r < 0.9:
            # 快速划动 -> flick
            dur = 0.08 + random.random() * 0.06
            path = [(t, x, 0.5), (t + dur, min(1.0, x + 0.25), 0.5)]
            notes.append({"gesture": FLICK, "finger": finger % 5, "t_sec": t + dur, "x": min(1.0, x + 0.25)})
            t += 0.3
        else:
            # 拖拽 -> 一段轨迹曲线
            path = []
            cx = x
            for k in range(8):
                cx = min(1.0, max(0.0, cx + (random.random() - 0.5) * 0.3))
                path.append((t + k * 0.05, cx, 0.5))
            notes.append({"gesture": DRAG, "finger": finger % 5, "t_sec": t, "x": x, "path": path})
            t += 0.5
        finger += 1
    return notes, clock


if __name__ == "__main__":
    notes, clk = simulate_session()
    settings = {"subdivision": 8, "snap_tolerance": 0.6, "debounce_ms": 40,
                "x_mode": "coordinate", "regions": 9, "drag_sample_ms": 25}
    meta = {"name": "Test", "charter": "me"}
    rpe, st = build_rpe(notes, clk, settings, meta)
    print("stats:", st)
    print("notes:", len(rpe["judgeLineList"][0]["notes"]))
    print("first note:", rpe["judgeLineList"][0]["notes"][0])
    print("types present:", sorted(set(n["type"] for n in rpe["judgeLineList"][0]["notes"])))
