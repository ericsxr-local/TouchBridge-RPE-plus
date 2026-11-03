"""engine.py 单元测试：量化 / 坐标映射 / 音符构建 / RPE 导出 / 四种类型。"""
import math
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import (BeatClock, quantize, map_x, convert_notes, build_rpe,
                    simulate_session, GestureTracker, TAP, HOLD, FLICK, DRAG, NOTE_TYPE)


def approx(a, b, eps=1e-6):
    return abs(a - b) <= eps


def test_beat_to_bbt():
    """RPE Beat = [整数拍, 分子, 分母]（全整数，值 = [0]+[1]/[2]，第一位不是小节！）。

    依据 cmdysj 官方 rpe-next 解析器 beatValue: beat[0] + beat[1]/beat[2]。
    """
    c = BeatClock(bpm=120, beats_per_bar=4)
    assert c.beat_to_bbt(0.0) == [0, 0, 1]
    assert c.beat_to_bbt(2.0) == [2, 0, 1]      # 整数拍 -> [2,0,1]，不是 [0,2,1]
    assert c.beat_to_bbt(1.5) == [1, 1, 2]      # 1 + 1/2
    assert c.beat_to_bbt(5.25) == [5, 1, 4]     # 5 + 1/4（不是 [1,5,4]）
    assert c.beat_to_bbt(0.125) == [0, 1, 8]
    # 自由拍位（非网格小数）也必须是合法整数三元组
    b = c.beat_to_bbt(3.3333333)
    assert all(isinstance(v, int) for v in b) and b[2] > 0
    assert abs((b[0] + b[1] / b[2]) - 3.3333333) < 1e-3
    print("  ✓ beat_to_bbt(RPE Beat [整数拍,分子,分母])")


def test_quantize():
    assert quantize(0.13, 8, 1.0) == 0.125, "tolerance=1 应吸附"
    assert approx(quantize(0.13, 8, 0.0), 0.13), "tolerance=0 不吸附"
    assert quantize(0.11, 8, 0.6) == 0.125, "0.11 离 0.125 在 0.6 格内"
    # tolerance=0.3 时窗口=0.0375：0.18 离 0.125 差 0.055 超过窗口 -> 保留自由位
    assert approx(quantize(0.18, 8, 0.3), 0.18), "0.18 在容差0.3下应保留自由位"
    assert quantize(0.13, 8, 0.3) == 0.125, "0.13 在容差0.3下仍吸附"
    # tolerance>0.5 意味着窗口覆盖整格 -> 必定吸附
    assert quantize(0.18, 8, 0.6) == 0.125, "tolerance=0.6 窗口覆盖整格，0.18 吸附到 0.125"
    print("  ✓ quantize")


def test_map_x():
    assert approx(map_x(0.0), -675.0)
    assert approx(map_x(1.0), 675.0)
    assert approx(map_x(0.5), 0.0)
    assert map_x(2.0) == 675.0, "超出被夹到边界"
    # 区域模式
    assert approx(map_x(0, "region", 9), -675.0 + (0.5/9)*1350.0)
    print("  ✓ map_x")


def test_clock_offset_and_bpm():
    c = BeatClock(bpm=120, offset_ms=500)
    # offset=0.5s 平移：music_time=0.5 -> beat 0
    assert approx(c.time_to_beat(0.5), 0.0)
    assert approx(c.time_to_beat(1.5), 2.0)  # 1 秒 = 2 拍@120
    # 变速
    c2 = BeatClock(bpm=120, segments=[{"startBeat": 0, "bpm": 120}, {"startBeat": 4, "bpm": 240}])
    # 0~4 拍 @120 => 2 秒；第 5 拍在变速后：beat4 时间=2s, 再 +1拍@240=0.25s => 2.25s
    assert approx(c2.beat_to_time(5), 2.25, 1e-4)
    print("  ✓ clock(offset + 变速)")


def test_build_rpe_all_types():
    clock = BeatClock(bpm=120)
    notes = [
        {"gesture": TAP, "finger": 1, "t_sec": 1.00, "x": 0.5},
        {"gesture": HOLD, "finger": 2, "t_sec": 2.00, "x": 0.3, "hold_sec": 0.6},
        {"gesture": FLICK, "finger": 3, "t_sec": 3.00, "x": 0.8},
        {"gesture": DRAG, "finger": 4, "t_sec": 4.00, "x": 0.2,
         "path": [(4.00, 0.2, 0.5), (4.05, 0.3, 0.5), (4.10, 0.4, 0.5), (4.15, 0.5, 0.5)]},
    ]
    settings = {"subdivision": 8, "snap_tolerance": 0.6, "debounce_ms": 40,
                "x_mode": "coordinate", "regions": 9, "drag_sample_ms": 25}
    meta = {"name": "UnitTest", "charter": "tester"}
    rpe, stats = build_rpe(notes, clock, settings, meta)
    out = rpe["judgeLineList"][0]["notes"]
    types = set(n["type"] for n in out)
    assert 1 in types and 2 in types and 3 in types and 4 in types, f"四种类型应齐全: {types}"
    # 字段完整性：RPE 音符时间字段为 startTime/endTime（Beat 三整数 [小节,分子,分母]）
    for n in out:
        assert "positionX" in n and "startTime" in n and "endTime" in n
        for k in ("startTime", "endTime"):
            b = n[k]
            assert isinstance(b, list) and len(b) == 3, f"{k} 应为 3 元组数组"
            assert all(isinstance(v, int) for v in b), f"{k} 必须全为整数: {b}"
            assert b[2] > 0, f"{k} 分母必须为正整数(否则 RPE 解析 0/0=NaN): {b}"
        # Beat 值还原 = b[0] + b[1]/b[2]（RPE 语义：Beat[0] 为整数拍），且为非负整数拍
        val = b[0] + b[1] / b[2]
        assert val >= 0, f"Beat 不应为负: {b}"
        assert -675 <= n["positionX"] <= 675
    # 判定线合规：必须带 eventLayers 与常驻 speedEvents
    jl = rpe["judgeLineList"][0]
    assert jl["eventLayers"] and isinstance(jl["eventLayers"][0], dict), "eventLayers[0] 不能是 null"
    layer = jl["eventLayers"][0]
    # speedEvents：官方默认速度 10（写成 1 会让音符流速极慢/扭曲）+ floorPosition，且按规范不带缓动字段
    sp = layer["speedEvents"][0]
    assert sp["value"] == 10.0 and sp["floorPosition"] == 0.0, f"speedEvent 缺 value/floorPosition: {sp}"
    assert "easingType" not in sp, "SpeedEvent 不应有 easingType"
    # 判定线结构对齐官方 createLine：rotateWithFather / isGif / extended 为空对象
    assert jl.get("rotateWithFather") is True, "判定线应带 rotateWithFather:true"
    assert jl.get("isGif") is False, "判定线应带 isGif:false"
    assert jl.get("extended") == {}, "extended 应为空对象 {}"
    for kind in ("moveXEvents", "moveYEvents", "rotateEvents", "alphaEvents"):
        ev = layer[kind][0]
        assert ev["easingType"] == 1, f"{kind} 缺 easingType（缺失会导致桌面版插值 NaN、音符变形）"
        assert "value" not in ev and ev["start"] == ev["end"], f"{kind} 应为常驻事件"
    for ev in layer["speedEvents"] + layer["moveXEvents"] + layer["alphaEvents"]:
        assert all(isinstance(v, int) for v in ev["startTime"] + ev["endTime"]), "事件时间必须为整数 Beat"
    # BPM 事件合规
    b0 = rpe["BPMList"][0]
    assert all(isinstance(v, int) for v in b0["startTime"]) and b0["startTime"][2] > 0
    assert b0["bpm"] == 120.0
    # Hold 的 endTime 应晚于 startTime（Beat 值 = [0]+[1]/[2]）
    def _beat_val(b):
        return b[0] + b[1] / b[2]
    for n in out:
        if n["type"] == 2:
            assert _beat_val(n["endTime"]) > _beat_val(n["startTime"]), "Hold 应有正时长"
    print(f"  ✓ build_rpe 四种类型齐全 stats={stats} notes={len(out)}")

    # 顺便确认模拟器能跑通（不强制含全部类型）
    simulate_session(duration_sec=6.0, bpm=120, seed=7)
    print("  ✓ simulate_session 可运行")


def test_flick_vs_drag_logic():
    # 慢速移动 -> drag；快速划动 -> flick（用 on_up 的速度判定在 app.py，这里验证阈值思路）
    # 直接构造音符验证 convert_notes 对 flick/drag 的映射
    clock = BeatClock(bpm=120)
    settings = {"subdivision": 8, "snap_tolerance": 0.6, "debounce_ms": 40,
                "x_mode": "coordinate", "regions": 9, "drag_sample_ms": 25}
    notes = [
        {"gesture": FLICK, "finger": 1, "t_sec": 1.0, "x": 0.8},
        {"gesture": DRAG, "finger": 2, "t_sec": 2.0, "x": 0.2,
         "path": [(2.0, 0.2, 0.5), (2.05, 0.3, 0.5), (2.1, 0.4, 0.5)]},
    ]
    out = convert_notes(notes, clock, settings)
    m = {n["type"]: n for n in out}
    assert m[3]["positionX"] == map_x(0.8)
    assert len([n for n in out if n["type"] == 4]) >= 2, "拖拽轨迹应展开成多个 drag 点"
    print("  ✓ flick/drag 映射")


def test_debounce():
    clock = BeatClock(bpm=120)
    settings = {"subdivision": 8, "snap_tolerance": 1.0, "debounce_ms": 40,
                "x_mode": "coordinate", "regions": 9, "drag_sample_ms": 25}
    notes = [
        {"gesture": TAP, "finger": 3, "t_sec": 1.000, "x": 0.5},
        {"gesture": TAP, "finger": 3, "t_sec": 1.030, "x": 0.5},  # 30ms 内同指 -> 去抖
        {"gesture": TAP, "finger": 4, "t_sec": 1.030, "x": 0.5},  # 不同指 -> 保留
    ]
    out = convert_notes(notes, clock, settings)
    taps = [n for n in out if n["type"] == 1]
    assert len(taps) == 2, f"去抖应去掉 1 个同指重复，实际 {len(taps)}"
    print("  ✓ debounce")


def test_gesture_tracker_classification():
    """验证基于 /down /move /up 的自分类：死区/Tap/Hold/Flick/Drag。"""
    clock = {"t": 0.0}
    settings = {"deadZone": 0.03, "holdMinTime": 0.2, "flickWindow": 0.2,
                "flick_speed": 2.0, "flickMaxDist": 0.25}
    tr = GestureTracker(lambda: clock["t"], settings)

    # 1) 轻点：down -> up(dur=100ms, maxDist=0.01) -> Tap
    clock["t"] = 1.0
    tr.on_down(1, 0.5, 0.5, 0.5, 4)
    tr.on_up(1, 100.0, 0.01)
    assert tr.completed[-1]["gesture"] == "tap", "短且不动 -> Tap"

    # 2) 长按：down -> up(dur=600ms, maxDist=0.01) -> Hold
    clock["t"] = 2.0
    tr.on_down(2, 0.3, 0.5, 0.5, 4)
    tr.on_up(2, 600.0, 0.01)
    h = tr.completed[-1]
    assert h["gesture"] == "hold" and h["hold_sec"] >= 0.5, "长且不动 -> Hold"

    # 3) 快速短划：down -> 快速 move -> up(dur=90ms, maxDist=0.2, peakVel 高) -> Flick(红键+方向)
    clock["t"] = 3.0
    tr.on_down(3, 0.1, 0.5, 0.5, 6)
    clock["t"] = 3.04
    tr.on_move(3, 0.30, 0.5, 0.5)   # seg=0.2, dt=0.04 -> vel=5.0
    tr.on_up(3, 90.0, 0.20)
    f = tr.completed[-1]
    assert f["gesture"] == "flick", f"快速短划应得 Flick，实际 {f['gesture']}"
    assert f["dir"][0] > 0.99, "Flick 方向应指向 +x"
    assert "dir" in f

    # 4) 慢速长移：down -> 慢 move -> up(dur=400ms, maxDist=0.2, peakVel 低) -> Drag(带轨迹)
    clock["t"] = 4.0
    tr.on_down(4, 0.2, 0.5, 0.5, 2)
    clock["t"] = 4.1
    tr.on_move(4, 0.3, 0.5, 0.5)    # vel=1.0
    clock["t"] = 4.2
    tr.on_move(4, 0.4, 0.5, 0.5)    # vel=1.0
    tr.on_up(4, 400.0, 0.20)
    d = tr.completed[-1]
    assert d["gesture"] == "drag", f"慢速长移应得 Drag，实际 {d['gesture']}"
    assert len(d["path"]) >= 3, "Drag 应保留轨迹点"

    print(f"  ✓ GestureTracker 分类(共 {len(tr.completed)} 个音符已验证)")
    return tr.completed


def test_slow_drift_becomes_hold():
    """长按时手指轻微漂移(平均速度过低)应归为 Hold，而非生成乱拖尾。"""
    settings = {"deadZone": 0.03, "holdMinTime": 0.2, "flickWindow": 0.2,
                "flick_speed": 2.0, "flickMaxDist": 0.25, "dragMinSpeed": 0.25, "dragMaxDur": 1.5}
    clock = {"t": 0.0}
    tr = GestureTracker(lambda: clock["t"], settings)
    clock["t"] = 1.0
    tr.on_down(5, 0.20, 0.5, 0.5, 2)     # 按下
    clock["t"] = 1.5
    tr.on_move(5, 0.25, 0.5, 0.5)         # 6 秒? 不，dt=0.5 -> vel=0.1（很慢）
    tr.on_up(5, 600.0, 0.05)              # dur=600ms, maxDist=0.05
    g = tr.completed[-1]
    assert g["gesture"] == "hold", f"慢速漂移应归为 Hold，实际 {g['gesture']}"
    print("  ✓ 慢速漂移 -> Hold（避免乱拖尾）")


def test_tap_uses_down_time():
    """Tap 命中时刻应取手指落下(t0)，而非抬起时间，避免系统性偏晚。"""
    settings = {"deadZone": 0.03, "holdMinTime": 0.2, "flickWindow": 0.2,
                "flick_speed": 2.0, "flickMaxDist": 0.25}
    clock = {"t": 0.0}
    tr = GestureTracker(lambda: clock["t"], settings)
    clock["t"] = 2.0
    tr.on_down(9, 0.5, 0.5, 0.5, 4)
    clock["t"] = 2.12                       # 抬起（晚 120ms）
    tr.on_tap(9, 0.5, 0.5, 120.0)
    assert abs(tr.completed[-1]["t_sec"] - 2.0) < 1e-9, "tap 应记录 down_t(2.0) 而非 2.12"
    print("  ✓ Tap 命中时刻取 down_t")


def test_hit_offset_shift():
    """采样补偿偏移应把命中时刻整体平移，使偏早/偏晚的音符回到节拍上。"""
    clock = BeatClock(bpm=120)
    # 用户本想敲在 4/8/12 拍，但录音链路偏早 0.25 拍 -> 实际命中 3.75/7.75/11.75 拍
    notes = [
        {"gesture": TAP, "finger": 1, "t_sec": 3.75 / 2, "x": 0.5},
        {"gesture": TAP, "finger": 2, "t_sec": 7.75 / 2, "x": 0.5},
        {"gesture": TAP, "finger": 3, "t_sec": 11.75 / 2, "x": 0.5},
    ]
    base = {"subdivision": 8, "snap_tolerance": 1.0, "debounce_ms": 0,
            "x_mode": "coordinate", "regions": 9, "drag_sample_ms": 25, "hit_offset_ms": 0}
    out0 = convert_notes(notes, clock, base)
    raw = sorted(n["startBeat"] for n in out0 if n["type"] == 1)
    for g, exp in zip(raw, [3.75, 7.75, 11.75]):
        assert abs(g - exp) < 1e-6, f"未补偿应在 {exp}，实际 {g}"
    shifted = dict(base); shifted["hit_offset_ms"] = 125.0   # +125ms 抵消偏早 0.25 拍
    out1 = convert_notes(notes, clock, shifted)
    got = sorted(n["startBeat"] for n in out1 if n["type"] == 1)
    for g, exp in zip(got, [4.0, 8.0, 12.0]):
        assert abs(g - exp) < 1e-6, f"补偿后应在整拍 {exp}，实际 {g}"
    print("  ✓ 采样补偿偏移把偏早音符拉回整拍")


def test_drag_points_are_merged_and_chronological():
    """同一量化拍上的 Drag 采样应合并，所有音符仍按时间升序。"""
    clock = BeatClock(bpm=120)
    settings = {"subdivision": 8, "snap_tolerance": 1.0, "debounce_ms": 0,
                "x_mode": "coordinate", "regions": 9, "drag_sample_ms": 0}
    notes = [
        {"gesture": HOLD, "finger": 1, "t_sec": 0.5, "x": 0.2, "hold_sec": 0.5},
        {"gesture": DRAG, "finger": 2, "t_sec": 1.0, "x": 0.1, "gid": 7,
         "path": [(1.00, 0.10, 0.5), (1.01, 0.20, 0.5),
                  (1.02, 0.30, 0.5), (1.08, 0.50, 0.5)]},
        {"gesture": TAP, "finger": 3, "t_sec": 1.5, "x": 0.8},
    ]
    out = convert_notes(notes, clock, settings)
    drags = [n for n in out if n["type"] == 4]
    assert len(drags) == 2, f"同拍 Drag 点应合并为 2 个时序点，实际 {len(drags)}"
    beats = [n["startBeat"] for n in out]
    assert beats == sorted(beats), f"音符必须按时间排列，实际 {beats}"
    print("  ✓ Drag 同拍合并 + 全局时间排序")


def test_auto_calibrate_formula():
    """自动校准：偏早 0.25 拍应估算出 +125ms 补偿（正数=延后，抵消偏早）。"""
    import math
    clock = BeatClock(bpm=120)   # 每拍 0.5s
    beats = [3.75, 7.75, 11.75]  # 整拍 - 0.25（偏早）
    fracs = []
    for b in beats:
        f = b - math.floor(b)
        if f > 0.5:
            f -= 1.0
        fracs.append(f)
    fracs.sort()
    bias = fracs[len(fracs) // 2]
    offset_ms = round(-bias * 0.5 * 1000.0, 1)
    assert offset_ms == 125.0, f"应估算 +125ms，实际 {offset_ms}"
    print("  ✓ 自动校准公式恢复偏早偏差")


if __name__ == "__main__":
    print("运行 engine 测试…")
    test_quantize()
    test_beat_to_bbt()
    test_map_x()
    test_clock_offset_and_bpm()
    test_build_rpe_all_types()
    test_flick_vs_drag_logic()
    test_debounce()
    test_gesture_tracker_classification()
    test_slow_drift_becomes_hold()
    test_tap_uses_down_time()
    test_hit_offset_shift()
    test_drag_points_are_merged_and_chronological()
    test_auto_calibrate_formula()
    print("全部通过 ✅")
