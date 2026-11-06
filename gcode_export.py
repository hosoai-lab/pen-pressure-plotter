"""描画ストローク -> G-code 変換 (GUI非依存)

座標系
  キャンバス: 左上原点, 右が +x, 下が +y (px)
  機器      : (origin_x, origin_y) を基準に X幅 x Y幅 の領域を使う (mm)

変換の流れ
  1. キャンバス全体を、機器の描画領域 (余白を除いた範囲) に写像する
  2. ストロークごとに、点間隔 step (mm) で制御点を取り直す      ... 粒度
  3. 補間: 制御点を直線で結ぶ / Catmull-Rom 曲線で滑らかに結び、
          各区間を spline_div 本の短い直線に分割する              ... 出力は G0/G1 のみ
  4. 筆圧を Z に変換して出力する (筆圧の弱い所は z_light、強い所は z_full)
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

SETTINGS_PATH = Path.home() / ".pen_pressure_canvas_gcode.json"

# ストロークのサンプル (t_ms, x, y, pressure, xtilt, ytilt) 内の位置
_X, _Y, _P = 1, 2, 3


class GCodeError(ValueError):
    """設定やキャンバスサイズが不正で変換できない。"""


@dataclass
class GCodeSettings:
    # --- 機器と配置 (mm) ---
    machine_w: float = 210.0  # X方向の幅
    machine_h: float = 148.0  # Y方向の幅
    origin_x: float = 0.0  # 描画領域の左下(機器座標)のオフセット
    origin_y: float = 0.0
    margin: float = 5.0  # 領域の内側に空ける余白
    fit_mode: str = "fit"  # fit: 縦横比を保って収める / stretch: 領域いっぱいに引き伸ばす
    flip_y: bool = True  # 画面の上を +Y にする
    # --- Z軸 (mm) ---
    z_up: float = 3.0  # ペンを上げた位置
    z_light: float = 0.0  # 最小筆圧のとき(ペン先が触れる位置)
    z_full: float = -1.0  # 最大筆圧のとき(押し込む位置)
    use_pressure_z: bool = True  # False なら常に z_light で描く
    p_lo: float = 0.0  # 筆圧の有効範囲 (この値以下は z_light)
    p_hi: float = 1.0  # (この値以上は z_full)
    # --- 分割・補間 ---
    step: float = 0.5  # 制御点の間隔 (mm)
    interp: str = "linear"  # linear / spline
    spline_div: int = 4  # 曲線補間で1区間を何本の直線に分けるか
    # --- 送り速度 (mm/min) ---
    feed_draw: float = 3000.0
    feed_travel: float = 6000.0
    feed_z: float = 1000.0
    # --- 出力 ---
    decimals: int = 3
    start_code: str = "G21 ; mm\nG90 ; absolute"
    end_code: str = "G0 X0 Y0"


def load_settings(path: Path = SETTINGS_PATH) -> GCodeSettings:
    s = GCodeSettings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return s
    if not isinstance(data, dict):
        return s
    for k, v in data.items():
        if hasattr(s, k):
            try:
                setattr(s, k, type(getattr(s, k))(v))
            except (TypeError, ValueError):
                pass
    return s


def save_settings(s: GCodeSettings, path: Path = SETTINGS_PATH) -> None:
    try:
        path.write_text(json.dumps(s.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- 座標変換
def make_mapper(canvas_w: float, canvas_h: float, s: GCodeSettings):
    """キャンバス座標(px) -> 機器座標(mm)。 (関数, x倍率, y倍率) を返す。"""
    ax = s.machine_w - 2 * s.margin
    ay = s.machine_h - 2 * s.margin
    if ax <= 0 or ay <= 0:
        raise GCodeError("余白が大きすぎて描画領域がありません")
    if canvas_w <= 0 or canvas_h <= 0:
        raise GCodeError("キャンバスサイズが不正です")
    if s.fit_mode == "stretch":
        sx, sy = ax / canvas_w, ay / canvas_h
        ox, oy = s.margin, s.margin
    else:
        k = min(ax / canvas_w, ay / canvas_h)
        sx = sy = k
        ox = s.margin + (ax - canvas_w * k) / 2
        oy = s.margin + (ay - canvas_h * k) / 2

    def to_mm(x: float, y: float) -> tuple[float, float]:
        mx = s.origin_x + ox + x * sx
        if s.flip_y:
            my = s.origin_y + oy + (canvas_h - y) * sy
        else:
            my = s.origin_y + oy + y * sy
        return mx, my

    return to_mm, sx, sy


def pressure_to_z(p: float, s: GCodeSettings) -> float:
    if not s.use_pressure_z:
        return s.z_light
    span = s.p_hi - s.p_lo
    t = 0.0 if span <= 1e-9 else (p - s.p_lo) / span
    t = min(max(t, 0.0), 1.0)
    return s.z_light + (s.z_full - s.z_light) * t


# ---------------------------------------------------------------- 分割・補間
def _dedupe(pts: list[tuple], eps: float = 1e-3) -> list[tuple]:
    out = [pts[0]]
    for q in pts[1:]:
        if math.hypot(q[0] - out[-1][0], q[1] - out[-1][1]) >= eps:
            out.append(q)
    return out


def resample(pts: list[tuple], step: float) -> list[tuple]:
    """折れ線を弧長 step ごとに取り直す。始点と終点は必ず残す。(x, y, p) の p は線形補間。"""
    if len(pts) < 2 or step <= 0:
        return list(pts)
    out = [pts[0]]
    next_d = step
    done = 0.0
    for a, b in zip(pts, pts[1:]):
        seg = math.hypot(b[0] - a[0], b[1] - a[1])
        if seg == 0:
            continue
        while done + seg >= next_d - 1e-12:
            t = (next_d - done) / seg
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t))
            next_d += step
        done += seg
    last = pts[-1]
    if math.hypot(last[0] - out[-1][0], last[1] - out[-1][1]) > 1e-6:
        out.append(last)
    return out


def _catmull_rom(p0, p1, p2, p3, u: float) -> tuple[float, float]:
    """centripetal Catmull-Rom。p1 -> p2 の区間を u (0..1) で評価する。"""

    def knot(t, a, b):
        return t + max(math.hypot(b[0] - a[0], b[1] - a[1]), 1e-9) ** 0.5

    t0 = 0.0
    t1 = knot(t0, p0, p1)
    t2 = knot(t1, p1, p2)
    t3 = knot(t2, p2, p3)
    t = t1 + (t2 - t1) * u

    def mix(a, b, ta, tb):
        w = (t - ta) / (tb - ta)
        return a[0] * (1 - w) + b[0] * w, a[1] * (1 - w) + b[1] * w

    a1 = mix(p0, p1, t0, t1)
    a2 = mix(p1, p2, t1, t2)
    a3 = mix(p2, p3, t2, t3)
    b1 = mix(a1, a2, t0, t2)
    b2 = mix(a2, a3, t1, t3)
    return mix(b1, b2, t1, t2)


def spline(pts: list[tuple], div: int) -> list[tuple]:
    """制御点を通る Catmull-Rom 曲線にして、各区間を div 本の直線に分割する。"""
    if len(pts) < 3 or div < 2:
        return list(pts)
    first = (2 * pts[0][0] - pts[1][0], 2 * pts[0][1] - pts[1][1])
    last = (2 * pts[-1][0] - pts[-2][0], 2 * pts[-1][1] - pts[-2][1])
    xy = [first] + [(x, y) for x, y, _ in pts] + [last]
    out = [pts[0]]
    for i in range(1, len(xy) - 2):
        p0, p1, p2, p3 = xy[i - 1], xy[i], xy[i + 1], xy[i + 2]
        pa, pb = pts[i - 1][2], pts[i][2]
        for k in range(1, div + 1):
            u = k / div
            x, y = _catmull_rom(p0, p1, p2, p3, u)
            out.append((x, y, pa + (pb - pa) * u))
    return out


def build_toolpaths(strokes, canvas_w: float, canvas_h: float, s: GCodeSettings):
    """ストローク群 -> 機器座標の経路群 [[(x_mm, y_mm, pressure), ...], ...] と (x倍率, y倍率)。"""
    to_mm, sx, sy = make_mapper(canvas_w, canvas_h, s)
    paths = []
    for stroke in strokes:
        if not stroke:
            continue
        pts = [(*to_mm(smp[_X], smp[_Y]), smp[_P]) for smp in stroke]
        pts = _dedupe(pts)
        pts = resample(pts, s.step)
        if s.interp == "spline":
            pts = spline(pts, s.spline_div)
        paths.append(pts)
    return paths, (sx, sy)


# ---------------------------------------------------------------- G-code 出力
def fmt_num(v: float, decimals: int) -> str:
    t = f"{v:.{decimals}f}"
    if "." in t:
        t = t.rstrip("0").rstrip(".")
    return "0" if t in ("-0", "") else t


def paths_to_gcode(paths, canvas_w: float, canvas_h: float, s: GCodeSettings):
    d = s.decimals

    def f(v: float) -> str:
        return fmt_num(v, d)

    lines = [
        "; Generated by pen_pressure_canvas",
        f"; canvas {canvas_w:.0f} x {canvas_h:.0f} px -> area {f(s.machine_w)} x {f(s.machine_h)} mm, margin {f(s.margin)} mm",
        f"; step {f(s.step)} mm, interpolation {s.interp}",
    ]
    lines += s.start_code.splitlines()
    lines.append(f"G0 Z{f(s.z_up)} F{f(s.feed_z)}")

    pos = (s.origin_x, s.origin_y)
    draw_len = travel_len = z_seconds = 0.0
    n_pts = g1_lines = n_strokes = 0
    zs = [s.z_up]

    for i, path in enumerate(paths):
        if not path:
            continue
        n_strokes += 1
        n_pts += len(path)
        x0, y0, p0 = path[0]
        z0 = pressure_to_z(p0, s)
        lines.append(f"; stroke {i}")
        lines.append(f"G0 X{f(x0)} Y{f(y0)} F{f(s.feed_travel)}")
        lines.append(f"G1 Z{f(z0)} F{f(s.feed_z)}")
        travel_len += math.hypot(x0 - pos[0], y0 - pos[1])
        z_seconds += 2 * abs(s.z_up - z0) / max(s.feed_z, 1.0) * 60.0
        zs.append(z0)

        px, py, zcur = x0, y0, z0
        need_feed = True
        for x, y, p in path[1:]:
            z = pressure_to_z(p, s)
            if f(x) == f(px) and f(y) == f(py) and f(z) == f(zcur):
                continue
            cmd = f"G1 X{f(x)} Y{f(y)}"
            if f(z) != f(zcur):
                cmd += f" Z{f(z)}"
            if need_feed:
                cmd += f" F{f(s.feed_draw)}"
                need_feed = False
            lines.append(cmd)
            g1_lines += 1
            draw_len += math.hypot(x - px, y - py)
            px, py, zcur = x, y, z
            zs.append(z)
        lines.append(f"G0 Z{f(s.z_up)} F{f(s.feed_z)}")
        pos = (px, py)

    lines += s.end_code.splitlines()

    warnings = []
    if n_strokes == 0:
        warnings.append("ストロークがありません")
    z_draw_top = max(s.z_light, s.z_full) if s.use_pressure_z else s.z_light
    if s.z_up <= z_draw_top:
        warnings.append("ペン上げZが描画Zより高くありません (ペンが紙から離れない可能性)")

    seconds = (
        draw_len / max(s.feed_draw, 1.0) * 60.0 + travel_len / max(s.feed_travel, 1.0) * 60.0 + z_seconds
    )
    stats = {
        "strokes": n_strokes,
        "points": n_pts,
        "g1_lines": g1_lines,
        "draw_len": draw_len,
        "travel_len": travel_len,
        "z_min": min(zs),
        "z_max": max(zs),
        "est_seconds": seconds,
        "warnings": warnings,
    }
    return "\n".join(lines) + "\n", stats


def generate(strokes, canvas_w: float, canvas_h: float, s: GCodeSettings):
    """(G-code文字列, 統計dict, 経路) を返す。設定が不正なら GCodeError。"""
    paths, scale = build_toolpaths(strokes, canvas_w, canvas_h, s)
    text, stats = paths_to_gcode(paths, canvas_w, canvas_h, s)
    stats["scale"] = scale
    return text, stats, paths
