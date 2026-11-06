#!/usr/bin/env python3
"""筆圧キャンバス (Wacom / Windows Ink 対応)

ペンタブレットで描いた線を、筆圧に応じた色で再現するお絵かきアプリ。
後でペンプロッタに筆圧を反映させるため、全サンプル (時刻, x, y, 筆圧, 傾き)
をストローク単位で保持し、CSV / JSON で書き出せる。

  pip install PySide6
  python pen_pressure_canvas.py      (gcode_export.py を同じフォルダに置く)

「G-code出力…」で、機器のサイズ・Z範囲・点間隔・補間方法などを設定して
G-code を書き出せる。設定は ~/.pen_pressure_canvas_gcode.json に保存される。

色の割り当て (HSV):
  筆圧 0.0 -> 青 (H=240°)  ...  筆圧 1.0 -> 赤 (H=0°)
  S=1, V=1 固定。色相は 240° の範囲だけ使うので、色から筆圧を逆算できる:
      pressure = 1 - H / 240
"""
import csv
import json
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPalette,
    QPen,
    QPointingDevice,
    QPolygonF,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from gcode_export import GCodeError, GCodeSettings, generate, load_settings, save_settings

# サンプルのタプル構造: (t_ms, x, y, pressure, xtilt, ytilt)
T, X, Y, P, TX, TY = range(6)

HUE_RANGE_DEG = 240.0  # 青(240°) -> 赤(0°)


def pressure_to_color(p: float, mode: str) -> QColor:
    """筆圧 0..1 を色に変換する。"""
    p = min(max(p, 0.0), 1.0)
    if mode == "gray":
        # 強い筆圧ほど濃く。0でも真っ白にならないよう上限を 0.85 に抑える
        return QColor.fromHsvF(0.0, 0.0, 0.85 * (1.0 - p))
    hue = (1.0 - p) * HUE_RANGE_DEG / 360.0
    return QColor.fromHsvF(hue, 1.0, 1.0)


class Canvas(QWidget):
    # 筆圧, x, y, xtilt, ytilt, 入力デバイス名
    sampled = Signal(float, float, float, float, float, str)
    settingsChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(640, 420)
        self.setTabletTracking(True)  # ペンを浮かせている間も位置・状態を受け取る
        self.setCursor(Qt.CursorShape.CrossCursor)

        self.strokes: list[list[tuple]] = []
        self._current: list[tuple] | None = None
        self._down = False
        self._t0: int | None = None
        self._cache: QImage | None = None

        self.color_mode = "hsv"
        self.base_width = 6.0
        self.width_by_pressure = False

    # ------------------------------------------------------------ 設定
    def set_color_mode(self, mode: str):
        self.color_mode = mode
        self._rebuild_cache()
        self.settingsChanged.emit()

    def set_base_width(self, w: float):
        self.base_width = float(w)
        self._rebuild_cache()

    def set_width_by_pressure(self, on: bool):
        self.width_by_pressure = on
        self._rebuild_cache()

    # ------------------------------------------------------------ 描画
    def _width(self, p: float) -> float:
        if self.width_by_pressure:
            return self.base_width * (0.25 + 1.5 * p)
        return self.base_width

    def _draw_dot(self, painter: QPainter, s: tuple):
        w = self._width(s[P])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(pressure_to_color(s[P], self.color_mode))
        painter.drawEllipse(QPointF(s[X], s[Y]), w / 2, w / 2)

    def _draw_segment(self, painter: QPainter, a: tuple, b: tuple):
        p = (a[P] + b[P]) / 2
        pen = QPen(
            pressure_to_color(p, self.color_mode),
            self._width(p),
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        painter.setPen(pen)
        painter.drawLine(QPointF(a[X], a[Y]), QPointF(b[X], b[Y]))

    def _paint_all(self, painter: QPainter, w: int, h: int):
        painter.fillRect(QRectF(0, 0, w, h), Qt.GlobalColor.white)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for stroke in self.strokes:
            self._draw_dot(painter, stroke[0])
            for a, b in zip(stroke, stroke[1:]):
                self._draw_segment(painter, a, b)

    def _rebuild_cache(self):
        dpr = self.devicePixelRatioF()
        img = QImage(
            max(1, int(self.width() * dpr)),
            max(1, int(self.height() * dpr)),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        img.setDevicePixelRatio(dpr)
        painter = QPainter(img)
        self._paint_all(painter, self.width(), self.height())
        painter.end()
        self._cache = img
        self.update()

    def _paint_incremental(self, prev: tuple | None, cur: tuple):
        if self._cache is None:
            self._rebuild_cache()
            return
        painter = QPainter(self._cache)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if prev is None:
            self._draw_dot(painter, cur)
            a = cur
        else:
            self._draw_segment(painter, prev, cur)
            a = prev
        painter.end()
        m = self._width(1.0) + 4
        rect = QRectF(QPointF(a[X], a[Y]), QPointF(cur[X], cur[Y])).normalized()
        self.update(rect.adjusted(-m, -m, m, m).toAlignedRect())

    def resizeEvent(self, e):
        self._rebuild_cache()

    def paintEvent(self, e):
        qp = QPainter(self)
        if self._cache is None:
            qp.fillRect(self.rect(), Qt.GlobalColor.white)
        else:
            qp.drawImage(0, 0, self._cache)

    # ------------------------------------------------------------ 入力
    def _feed(self, pos: QPointF, p: float, tx: float, ty: float, ts: int, dev: str):
        self.sampled.emit(p, pos.x(), pos.y(), tx, ty, dev)
        if p <= 0:  # 接触前後の筆圧0のサンプルは線に含めない
            return
        if self._t0 is None:
            self._t0 = ts
        sample = (int(ts - self._t0), pos.x(), pos.y(), p, tx, ty)
        if self._current is None:
            self._current = [sample]
            self.strokes.append(self._current)
            self._paint_incremental(None, sample)
        else:
            prev = self._current[-1]
            self._current.append(sample)
            self._paint_incremental(prev, sample)

    def _on_down(self, pos, p, tx, ty, ts, dev):
        self._down = True
        self._current = None
        self._feed(pos, p, tx, ty, ts, dev)

    def _on_move(self, pos, p, tx, ty, ts, dev):
        if self._down:
            self._feed(pos, p, tx, ty, ts, dev)
        else:  # ホバー中: 状態表示だけ更新
            self.sampled.emit(0.0, pos.x(), pos.y(), tx, ty, dev)

    def _on_up(self, pos, tx, ty, dev):
        self._down = False
        self._current = None
        self.sampled.emit(0.0, pos.x(), pos.y(), tx, ty, dev)

    def tabletEvent(self, e):
        e.accept()  # 受理するとマウスイベントに変換されない
        if e.pointerType() == QPointingDevice.PointerType.Eraser:
            return
        t = e.type()
        pos = e.position()
        args = (pos, e.pressure(), e.xTilt(), e.yTilt(), e.timestamp(), "ペン")
        if t == QEvent.Type.TabletPress:
            self._on_down(*args)
        elif t == QEvent.Type.TabletMove:
            self._on_move(*args)
        elif t == QEvent.Type.TabletRelease:
            self._on_up(pos, e.xTilt(), e.yTilt(), "ペン")

    # タブレットが無い環境での動作確認用 (筆圧は 0.5 固定)
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._on_down(e.position(), 0.5, 0.0, 0.0, e.timestamp(), "マウス(筆圧0.5固定)")

    def mouseMoveEvent(self, e):
        self._on_move(e.position(), 0.5, 0.0, 0.0, e.timestamp(), "マウス(筆圧0.5固定)")

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._on_up(e.position(), 0.0, 0.0, "マウス(筆圧0.5固定)")

    # ------------------------------------------------------------ 操作
    def undo(self):
        if self._down or not self.strokes:
            return
        self.strokes.pop()
        self._rebuild_cache()

    def clear(self):
        if self._down:
            return
        self.strokes.clear()
        self._t0 = None
        self._rebuild_cache()

    # ------------------------------------------------------------ 書き出し
    def render_image(self) -> QImage:
        img = QImage(self.width(), self.height(), QImage.Format.Format_ARGB32_Premultiplied)
        painter = QPainter(img)
        self._paint_all(painter, self.width(), self.height())
        painter.end()
        return img

    def export_csv(self, path: str):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["stroke", "t_ms", "x_px", "y_px", "pressure", "xtilt_deg", "ytilt_deg"])
            for i, stroke in enumerate(self.strokes):
                for t, x, y, p, tx, ty in stroke:
                    w.writerow([i, t, f"{x:.2f}", f"{y:.2f}", f"{p:.4f}", tx, ty])

    def export_json(self, path: str):
        data = {
            "version": 1,
            "canvas": {"width": self.width(), "height": self.height(), "unit": "px"},
            "columns": ["t_ms", "x_px", "y_px", "pressure", "xtilt_deg", "ytilt_deg"],
            "strokes": [
                [[t, round(x, 2), round(y, 2), round(p, 4), tx, ty] for t, x, y, p, tx, ty in s]
                for s in self.strokes
            ],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)


class ColorBar(QWidget):
    """筆圧と色の対応(凡例)と、現在の筆圧マーカー。"""

    def __init__(self, canvas: Canvas):
        super().__init__()
        self.canvas = canvas
        self.pressure = 0.0
        self.setFixedWidth(72)
        canvas.settingsChanged.connect(self.update)

    def set_pressure(self, p: float):
        self.pressure = p
        self.update()

    def paintEvent(self, e):
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        fg = self.palette().color(QPalette.ColorRole.WindowText)
        top, bottom = 30, 16
        bar = QRectF(14, top, 18, max(10, self.height() - top - bottom))

        grad = QLinearGradient(bar.topLeft(), bar.bottomLeft())
        for i in range(11):
            grad.setColorAt(i / 10, pressure_to_color(1.0 - i / 10, self.canvas.color_mode))
        qp.fillRect(bar, QBrush(grad))

        qp.setPen(QPen(fg, 1))
        qp.drawRect(bar)
        qp.drawText(QPointF(10, 18), "筆圧")
        for v in (0.0, 0.25, 0.5, 0.75, 1.0):
            y = bar.bottom() - v * bar.height()
            qp.drawLine(QPointF(bar.right(), y), QPointF(bar.right() + 4, y))
            qp.drawText(QPointF(bar.right() + 7, y + 4), f"{v:.2f}")

        if self.pressure > 0:
            y = bar.bottom() - self.pressure * bar.height()
            tri = QPolygonF(
                [QPointF(bar.left() - 2, y), QPointF(bar.left() - 10, y - 5), QPointF(bar.left() - 10, y + 5)]
            )
            qp.setBrush(fg)
            qp.drawPolygon(tri)


def _dspin(value, lo, hi, step=1.0, decimals=2, suffix=""):
    w = QDoubleSpinBox()
    w.setRange(lo, hi)
    w.setDecimals(decimals)
    w.setSingleStep(step)
    w.setSuffix(suffix)
    w.setValue(value)
    return w


def _ispin(value, lo, hi, suffix=""):
    w = QSpinBox()
    w.setRange(lo, hi)
    w.setSuffix(suffix)
    w.setValue(value)
    return w


def _combo(items, current):
    w = QComboBox()
    for text, data in items:
        w.addItem(text, data)
    w.setCurrentIndex(max(w.findData(current), 0))
    return w


def _textbox(value, lines=2):
    w = QPlainTextEdit()
    w.setPlainText(value)
    w.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    w.setFixedHeight(w.fontMetrics().lineSpacing() * lines + 14)
    return w


def _read(w):
    if isinstance(w, (QDoubleSpinBox, QSpinBox)):
        return w.value()
    if isinstance(w, QCheckBox):
        return w.isChecked()
    if isinstance(w, QComboBox):
        return w.currentData()
    return w.toPlainText()


def _fmt_time(sec: float) -> str:
    m, s = divmod(int(round(sec)), 60)
    h, m = divmod(m, 60)
    return f"{h}時間{m}分{s}秒" if h else f"{m}分{s}秒"


class ToolpathPreview(QWidget):
    """機器の描画領域と、出力される軌跡 (筆圧の色つき) を機器座標で表示する。"""

    def __init__(self):
        super().__init__()
        self.setMinimumSize(380, 300)
        self.paths = []
        self.s = GCodeSettings()

    def set_data(self, paths, s):
        self.paths = paths
        self.s = s
        self.update()

    def paintEvent(self, e):
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        qp.fillRect(self.rect(), pal.color(QPalette.ColorRole.Base))
        s = self.s
        pad = 14
        aw, ah = self.width() - 2 * pad, self.height() - 2 * pad
        if aw <= 0 or ah <= 0 or s.machine_w <= 0 or s.machine_h <= 0:
            return
        k = min(aw / s.machine_w, ah / s.machine_h)
        left = pad + (aw - s.machine_w * k) / 2
        top = pad + (ah - s.machine_h * k) / 2

        def tp(x, y):  # 機器座標 (Y上向き) -> 画面
            return QPointF(left + (x - s.origin_x) * k, top + (s.machine_h - (y - s.origin_y)) * k)

        qp.setPen(QPen(pal.color(QPalette.ColorRole.WindowText), 1))
        qp.setBrush(Qt.GlobalColor.white)
        qp.drawRect(QRectF(left, top, s.machine_w * k, s.machine_h * k))
        if s.machine_w > 2 * s.margin and s.machine_h > 2 * s.margin:
            qp.setBrush(Qt.BrushStyle.NoBrush)
            qp.setPen(QPen(QColor(160, 160, 160), 1, Qt.PenStyle.DashLine))
            qp.drawRect(
                QRectF(left + s.margin * k, top + s.margin * k, (s.machine_w - 2 * s.margin) * k, (s.machine_h - 2 * s.margin) * k)
            )

        prev = None
        for path in self.paths:
            if not path:
                continue
            if prev is not None:  # ペンを上げた移動
                qp.setPen(QPen(QColor(190, 190, 190), 1, Qt.PenStyle.DotLine))
                qp.drawLine(tp(prev[0], prev[1]), tp(path[0][0], path[0][1]))
            if len(path) == 1:
                qp.setPen(Qt.PenStyle.NoPen)
                qp.setBrush(pressure_to_color(path[0][2], "hsv"))
                qp.drawEllipse(tp(path[0][0], path[0][1]), 2.0, 2.0)
            for a, b in zip(path, path[1:]):
                pen = QPen(pressure_to_color((a[2] + b[2]) / 2, "hsv"), 1.6)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                qp.setPen(pen)
                qp.drawLine(tp(a[0], a[1]), tp(b[0], b[1]))
            prev = path[-1]


class GCodeDialog(QDialog):
    """G-code 出力の設定。変更するとすぐ右側のプレビューと統計に反映される。"""

    def __init__(self, parent, strokes, canvas_w, canvas_h):
        super().__init__(parent)
        self.setWindowTitle("G-code 出力")
        self.strokes = strokes
        self.canvas_w = canvas_w
        self.canvas_h = canvas_h
        self.saved_path = None
        self._text = None
        self.w = {}
        s = load_settings()

        def group(title):
            box = QGroupBox(title)
            form = QFormLayout(box)
            return box, form

        def add(form, key, label, widget):
            self.w[key] = widget
            form.addRow(label, widget)

        g1, f1 = group("機器と配置")
        add(f1, "machine_w", "X幅", _dspin(s.machine_w, 10, 5000, 10, 1, " mm"))
        add(f1, "machine_h", "Y幅", _dspin(s.machine_h, 10, 5000, 10, 1, " mm"))
        add(f1, "origin_x", "原点オフセット X", _dspin(s.origin_x, -5000, 5000, 1, 1, " mm"))
        add(f1, "origin_y", "原点オフセット Y", _dspin(s.origin_y, -5000, 5000, 1, 1, " mm"))
        add(f1, "margin", "余白", _dspin(s.margin, 0, 500, 1, 1, " mm"))
        add(
            f1,
            "fit_mode",
            "配置",
            _combo([("縦横比を保って収める", "fit"), ("縦横それぞれ引き伸ばす", "stretch")], s.fit_mode),
        )
        add(f1, "flip_y", "", _chk("画面の上を +Y にする", s.flip_y))

        g2, f2 = group("Z軸 (ペンの上下)")
        add(f2, "z_up", "ペン上げ位置", _dspin(s.z_up, -500, 500, 0.5, 2, " mm"))
        add(f2, "z_light", "最小筆圧のZ (接地)", _dspin(s.z_light, -500, 500, 0.1, 2, " mm"))
        add(f2, "z_full", "最大筆圧のZ (押し込み)", _dspin(s.z_full, -500, 500, 0.1, 2, " mm"))
        add(f2, "use_pressure_z", "", _chk("筆圧をZに反映する", s.use_pressure_z))
        add(f2, "p_lo", "筆圧の有効範囲 下限", _dspin(s.p_lo, 0, 1, 0.05, 2))
        add(f2, "p_hi", "筆圧の有効範囲 上限", _dspin(s.p_hi, 0, 1, 0.05, 2))

        g3, f3 = group("分割と補間")
        add(f3, "step", "点間隔 (粒度)", _dspin(s.step, 0.05, 50, 0.1, 2, " mm"))
        add(
            f3,
            "interp",
            "補間",
            _combo([("直線 (制御点をG1で結ぶ)", "linear"), ("Catmull-Rom曲線 (細かいG1に分割)", "spline")], s.interp),
        )
        add(f3, "spline_div", "曲線の分割数 (1区間あたり)", _ispin(s.spline_div, 2, 32, " 本"))

        g4, f4 = group("送り速度")
        add(f4, "feed_draw", "描画", _dspin(s.feed_draw, 1, 100000, 100, 0, " mm/min"))
        add(f4, "feed_travel", "移動 (ペン上げ)", _dspin(s.feed_travel, 1, 100000, 100, 0, " mm/min"))
        add(f4, "feed_z", "Z上下", _dspin(s.feed_z, 1, 100000, 100, 0, " mm/min"))

        g5, f5 = group("出力")
        add(f5, "decimals", "小数桁数", _ispin(s.decimals, 0, 5))
        add(f5, "start_code", "開始コード", _textbox(s.start_code, 3))
        add(f5, "end_code", "終了コード", _textbox(s.end_code, 3))

        inner = QWidget()
        left_col = QVBoxLayout(inner)
        for g in (g1, g2, g3, g4, g5):
            left_col.addWidget(g)
        left_col.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(420)

        self.preview = ToolpathPreview()
        self.stats = QLabel()
        self.stats.setWordWrap(True)
        self.stats.setTextFormat(Qt.TextFormat.PlainText)
        self.save_btn = QPushButton("G-codeを保存…")
        self.save_btn.setDefault(True)
        close_btn = QPushButton("閉じる")
        btns = QHBoxLayout()
        btns.addStretch(1)
        btns.addWidget(self.save_btn)
        btns.addWidget(close_btn)

        right = QVBoxLayout()
        right.addWidget(self.preview, 1)
        right.addWidget(self.stats)
        right.addLayout(btns)

        root = QHBoxLayout(self)
        root.addWidget(scroll, 0)
        root.addLayout(right, 1)
        self.resize(1040, 740)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._refresh)
        for wd in self.w.values():
            if isinstance(wd, (QDoubleSpinBox, QSpinBox)):
                wd.valueChanged.connect(self._schedule)
            elif isinstance(wd, QCheckBox):
                wd.toggled.connect(self._schedule)
            elif isinstance(wd, QComboBox):
                wd.currentIndexChanged.connect(self._schedule)
            else:
                wd.textChanged.connect(self._schedule)
        self.save_btn.clicked.connect(self._save)
        close_btn.clicked.connect(self.reject)
        self._refresh()

    def settings(self) -> GCodeSettings:
        return GCodeSettings(**{k: _read(wd) for k, wd in self.w.items()})

    def _schedule(self, *_):
        self._timer.start()

    def _refresh(self):
        s = self.settings()
        self.w["spline_div"].setEnabled(s.interp == "spline")
        for key in ("z_full", "p_lo", "p_hi"):
            self.w[key].setEnabled(s.use_pressure_z)
        try:
            text, st, paths = generate(self.strokes, self.canvas_w, self.canvas_h, s)
        except GCodeError as ex:
            self._text = None
            self.preview.set_data([], s)
            self.stats.setText(f"エラー: {ex}")
            self.save_btn.setEnabled(False)
            return
        self._text = text
        self.preview.set_data(paths, s)
        sx, sy = st["scale"]
        msg = [
            f"ストローク {st['strokes']} 本 / 出力点 {st['points']} / G1 {st['g1_lines']} 行",
            f"描画 {st['draw_len']:.1f} mm / ペン上げ移動 {st['travel_len']:.1f} mm / 概算 {_fmt_time(st['est_seconds'])}",
            f"Z {st['z_min']:.2f} 〜 {st['z_max']:.2f} mm / キャンバス {self.canvas_w}×{self.canvas_h} px → "
            + (f"{sx:.4f} mm/px" if abs(sx - sy) < 1e-9 else f"X {sx:.4f}, Y {sy:.4f} mm/px"),
        ]
        msg += [f"注意: {w}" for w in st["warnings"]]
        self.stats.setText("\n".join(msg))
        self.save_btn.setEnabled(st["strokes"] > 0)

    def _save(self):
        if not self._text:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "G-codeを保存", "pressure_drawing.gcode", "G-code (*.gcode *.nc *.gc);;すべてのファイル (*)"
        )
        if not path:
            return
        if not Path(path).suffix:
            path += ".gcode"
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(self._text)
        self.saved_path = path
        self.accept()

    def done(self, r):
        save_settings(self.settings())  # 次回起動時も同じ設定で開く
        super().done(r)


def _chk(text, checked):
    w = QCheckBox(text)
    w.setChecked(checked)
    return w


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("筆圧キャンバス")

        self.canvas = Canvas()
        self.bar = ColorBar(self.canvas)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("HSV (青→赤)", "hsv")
        self.mode_combo.addItem("グレースケール", "gray")

        self.width_slider = QSlider(Qt.Orientation.Horizontal)
        self.width_slider.setRange(1, 40)
        self.width_slider.setValue(int(self.canvas.base_width))
        self.width_slider.setFixedWidth(140)
        self.width_label = QLabel(f"{self.width_slider.value()} px")
        self.width_label.setMinimumWidth(40)

        self.width_check = QCheckBox("筆圧で太さも変える")

        undo_btn = QPushButton("取り消し")
        undo_btn.setToolTip("Ctrl+Z")
        clear_btn = QPushButton("全消去")
        png_btn = QPushButton("画像保存…")
        data_btn = QPushButton("CSV/JSON保存…")
        gcode_btn = QPushButton("G-code出力…")

        tools = QHBoxLayout()
        tools.addWidget(QLabel("色表現"))
        tools.addWidget(self.mode_combo)
        tools.addSpacing(12)
        tools.addWidget(QLabel("線の太さ"))
        tools.addWidget(self.width_slider)
        tools.addWidget(self.width_label)
        tools.addWidget(self.width_check)
        tools.addStretch(1)
        tools.addWidget(undo_btn)
        tools.addWidget(clear_btn)
        tools.addWidget(png_btn)
        tools.addWidget(data_btn)
        tools.addWidget(gcode_btn)

        body = QHBoxLayout()
        body.addWidget(self.canvas, 1)
        body.addWidget(self.bar)

        root = QVBoxLayout()
        root.addLayout(tools)
        root.addLayout(body, 1)
        central = QWidget()
        central.setLayout(root)
        self.setCentralWidget(central)

        self.mode_combo.currentIndexChanged.connect(
            lambda _: self.canvas.set_color_mode(self.mode_combo.currentData())
        )
        self.width_slider.valueChanged.connect(self._on_width)
        self.width_check.toggled.connect(self.canvas.set_width_by_pressure)
        undo_btn.clicked.connect(self.canvas.undo)
        clear_btn.clicked.connect(self.canvas.clear)
        png_btn.clicked.connect(self.save_image)
        data_btn.clicked.connect(self.save_data)
        gcode_btn.clicked.connect(self.open_gcode)
        self.canvas.sampled.connect(self.on_sampled)
        QShortcut(QKeySequence("Ctrl+Z"), self, activated=self.canvas.undo)

        self.statusBar().showMessage("ペンで描いてください。ペンを近づけると位置・傾きが表示されます。")

    def _on_width(self, v: int):
        self.width_label.setText(f"{v} px")
        self.canvas.set_base_width(v)

    def on_sampled(self, p, x, y, tx, ty, dev):
        self.bar.set_pressure(p)
        self.statusBar().showMessage(
            f"筆圧 {p:5.3f}    位置 ({x:6.1f}, {y:6.1f}) px    "
            f"傾き X {tx:+4.0f}° Y {ty:+4.0f}°    入力: {dev}    "
            f"ストローク数: {len(self.canvas.strokes)}"
        )

    def open_gcode(self):
        dlg = GCodeDialog(self, self.canvas.strokes, self.canvas.width(), self.canvas.height())
        dlg.exec()
        if dlg.saved_path:
            self.statusBar().showMessage(f"G-codeを保存しました: {dlg.saved_path}")

    def save_image(self):
        path, _ = QFileDialog.getSaveFileName(self, "画像を保存", "pressure_drawing.png", "PNG (*.png)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        self.canvas.render_image().save(path)
        self.statusBar().showMessage(f"画像を保存しました: {path}")

    def save_data(self):
        path, flt = QFileDialog.getSaveFileName(
            self, "データを保存", "pressure_drawing.csv", "CSV (*.csv);;JSON (*.json)"
        )
        if not path:
            return
        ext = Path(path).suffix.lower()
        if ext not in (".csv", ".json"):
            ext = ".json" if "json" in flt.lower() else ".csv"
            path += ext
        if ext == ".json":
            self.canvas.export_json(path)
        else:
            self.canvas.export_csv(path)
        self.statusBar().showMessage(f"データを保存しました: {path}")


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.resize(1100, 760)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
