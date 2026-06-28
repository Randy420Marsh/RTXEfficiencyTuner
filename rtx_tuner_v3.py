import sys
import subprocess
import pynvml
import platform
import collections
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
                             QLabel, QGroupBox, QTabWidget, QSpinBox,
                             QPushButton, QComboBox, QMessageBox, QTableWidget,
                             QTableWidgetItem, QHeaderView, QSlider, QScrollArea,
                             QSizePolicy)
from PyQt6.QtCore import QTimer, Qt, QPointF, pyqtSignal
from PyQt6.QtGui import QPainter, QPen, QColor, QBrush, QPolygonF, QFont

# Throttle reason bitmask -> human label
THROTTLE_REASONS = {
    0x0000000000000002: "App Clock",
    0x0000000000000004: "SW Power Cap",
    0x0000000000000008: "HW Slowdown",
    0x0000000000000020: "SW Thermal",
    0x0000000000000040: "HW Thermal",
    0x0000000000000080: "Power Brake",
}

# --- GRAPHING COMPONENT ---
class LiveGraph(QWidget):
    """
    A rolling 60-second line graph.
    """
    def __init__(self, label, color, max_val=100, unit="%", ref_line=None, parent=None):
        super().__init__(parent)
        self.setMinimumSize(400, 150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.data = collections.deque([0] * 60, maxlen=60)
        self.label = label
        self.color = color
        self.max_val = max(max_val, 1)
        self.unit = unit
        self.ref_line = ref_line
        self.display_text = "--"

    def update_data(self, plot_value, display_text):
        self.data.append(plot_value)
        self.display_text = display_text
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(30, 31, 46))

        w, h = self.width(), self.height()
        TOP_PAD = 28

        painter.setPen(QPen(QColor(169, 177, 214), 1))
        painter.setFont(QFont("monospace", 9))
        painter.drawText(10, 18, f"{self.label}: {self.display_text}")

        if self.ref_line is not None and self.max_val > 0:
            ref_y = h - TOP_PAD - ((self.ref_line / self.max_val) * (h - TOP_PAD))
            ref_y = max(TOP_PAD, min(h, ref_y))
            pen = QPen(QColor(247, 118, 142), 1, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawLine(0, int(ref_y), w, int(ref_y))
            painter.setPen(QPen(QColor(247, 118, 142), 1))
            painter.drawText(w - 60, int(ref_y) - 3, f"max {self.ref_line}{self.unit}")

        if len(self.data) < 2:
            return

        path = QPolygonF()
        for i, val in enumerate(self.data):
            x = (i / 59) * w
            normalized = val / self.max_val
            clamped = max(0.0, min(1.0, normalized))
            y = h - clamped * (h - TOP_PAD)
            path.append(QPointF(x, y))

        painter.setPen(QPen(self.color, 2))
        painter.drawPolyline(path)


# --- FAN CURVE COMPONENT ---
class FanCurveGraph(QWidget):
    curveChanged = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(450, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.points = [[10, 20], [40, 40], [60, 60], [80, 100]]
        self.selected_point_idx = None
        self.margin = 40
        self.setMouseTracking(True)

    def get_coords(self, temp, fan):
        draw_w = self.width()  - 2 * self.margin
        draw_h = self.height() - 2 * self.margin
        px = self.margin + (temp / 100) * draw_w
        py = self.height() - self.margin - (fan / 100) * draw_h
        return px, py

    def get_values(self, px, py):
        draw_w = self.width()  - 2 * self.margin
        draw_h = self.height() - 2 * self.margin
        temp = ((px - self.margin) / draw_w) * 100
        fan  = ((self.height() - self.margin - py) / draw_h) * 100
        return int(max(0, min(100, temp))), int(max(0, min(100, fan)))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(26, 27, 38))

        # Grid lines + axis labels
        painter.setFont(QFont("monospace", 8))
        for i in range(0, 101, 20):
            x, y = self.get_coords(i, i)
            # vertical
            painter.setPen(QPen(QColor(69, 71, 90), 1, Qt.PenStyle.DotLine))
            painter.drawLine(int(x), self.margin, int(x), self.height() - self.margin)
            # horizontal
            painter.drawLine(self.margin, int(y), self.width() - self.margin, int(y))
            # axis labels
            painter.setPen(QPen(QColor(101, 108, 140), 1))
            painter.drawText(int(x) - 8, self.height() - self.margin + 14, f"{i}")
            painter.drawText(2, int(y) + 4, f"{i}")

        # Axis titles
        painter.setPen(QPen(QColor(169, 177, 214), 1))
        painter.setFont(QFont("monospace", 9))
        painter.drawText(self.width() // 2 - 25, self.height() - 2, "Temp °C")
        # Rotated "Fan %" label
        painter.save()
        painter.translate(10, self.height() // 2 + 20)
        painter.rotate(-90)
        painter.drawText(0, 0, "Fan %")
        painter.restore()

        # Interpolated smooth curve (linear segments between set points)
        sorted_pts = sorted(self.points)
        path = QPolygonF()
        for t, s in sorted_pts:
            px, py = self.get_coords(t, s)
            path.append(QPointF(px, py))
        painter.setPen(QPen(QColor(122, 162, 247), 3))
        painter.drawPolyline(path)

        # Draw set-point dots with labels
        for i, (t, s) in enumerate(self.points):
            px, py = self.get_coords(t, s)
            color = QColor(247, 118, 142) if i == self.selected_point_idx else QColor(158, 206, 106)
            painter.setBrush(QBrush(color))
            painter.setPen(QPen(QColor(26, 27, 38), 1))
            painter.drawEllipse(QPointF(px, py), 7, 7)
            # Always show value labels next to each point
            painter.setPen(QPen(QColor(220, 220, 220), 1))
            painter.setFont(QFont("monospace", 8))
            painter.drawText(int(px) + 10, int(py) - 4, f"{t}°/{s}%")

    def mousePressEvent(self, event):
        for i, (t, s) in enumerate(self.points):
            px, py = self.get_coords(t, s)
            if (QPointF(px, py) - event.position()).manhattanLength() < 20:
                self.selected_point_idx = i
                return

    def mouseMoveEvent(self, event):
        if self.selected_point_idx is not None:
            t_new, f_new = self.get_values(event.position().x(), event.position().y())
            t_min = self.points[self.selected_point_idx - 1][0] + 1 if self.selected_point_idx > 0 else 0
            t_max = self.points[self.selected_point_idx + 1][0] - 1 if self.selected_point_idx < len(self.points) - 1 else 100
            f_min = self.points[self.selected_point_idx - 1][1] if self.selected_point_idx > 0 else 0
            f_max = self.points[self.selected_point_idx + 1][1] if self.selected_point_idx < len(self.points) - 1 else 100

            self.points[self.selected_point_idx] = [
                max(t_min, min(t_max, t_new)),
                max(f_min, min(f_max, f_new)),
            ]
            self.update()
            self.curveChanged.emit(self.points)

    def mouseReleaseEvent(self, event):
        self.selected_point_idx = None


class AdvancedRTXTuner(QWidget):
    def __init__(self):
        super().__init__()
        pynvml.nvmlInit()
        self.device_count = pynvml.nvmlDeviceGetCount()
        self.current_gpu_index = 0
        self.handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        self.last_applied_fan = -1
        self._sudo_ok = True
        self._sudo_warned = False
        self._init_clock_limits()
        self.initUI()

        # Populate defaults from actual GPU state after UI is built
        self._load_gpu_defaults()

        # Prime sudo credentials on startup so the user is prompted now,
        # not minutes later when the fan curve is actively running.
        subprocess.run(["sudo", "-v"], check=False)

        # Refresh credentials every 4 minutes — the default sudo cache is 5 minutes,
        # so this keeps them alive indefinitely while the app is open.
        self._sudo_timer = QTimer(self)
        self._sudo_timer.timeout.connect(self._refresh_sudo)
        self._sudo_timer.start(4 * 60 * 1000)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_loop)
        self.timer.start(1000)

    def _init_clock_limits(self):
        """Cache the max boost clocks for graph scaling and throttle reference lines."""
        try:
            self.max_core_clk = pynvml.nvmlDeviceGetMaxClockInfo(self.handle, pynvml.NVML_CLOCK_GRAPHICS)
        except Exception:
            self.max_core_clk = 2500
        try:
            self.max_mem_clk = pynvml.nvmlDeviceGetMaxClockInfo(self.handle, pynvml.NVML_CLOCK_MEM)
        except Exception:
            self.max_mem_clk = 10000

    def _load_gpu_defaults(self):
        """
        Read actual current GPU settings and populate the UI controls so the
        displayed values always reflect the real hardware state on launch and
        whenever the selected GPU changes.
        """
        # --- Power limit: set spin to actual current limit, not a hardcoded default ---
        try:
            curr_pl = pynvml.nvmlDeviceGetPowerManagementLimit(self.handle) // 1000
            p_min, p_max = pynvml.nvmlDeviceGetPowerManagementLimitConstraints(self.handle)
            self.spin_pwr.setRange(p_min // 1000, p_max // 1000)
            self.lbl_pwr_range.setText(f"{p_min // 1000}W – {p_max // 1000}W")
            self.spin_pwr.setValue(curr_pl)
        except Exception:
            pass  # refresh_gpu_limits() already handles the fallback display

        # --- Core / mem offsets: query current values if nvidia-settings is available ---
        try:
            idx = self.current_gpu_index
            core_off = subprocess.check_output(
                f"nvidia-settings -t -q '[gpu:{idx}]/GPUGraphicsClockOffsetAllPerformanceLevels'",
                shell=True, text=True, stderr=subprocess.DEVNULL,
            ).strip()
            if core_off.lstrip("-").isdigit():
                self.spin_core.setValue(int(core_off))
        except Exception:
            pass

        try:
            idx = self.current_gpu_index
            mem_off = subprocess.check_output(
                f"nvidia-settings -t -q '[gpu:{idx}]/GPUMemoryTransferRateOffsetAllPerformanceLevels'",
                shell=True, text=True, stderr=subprocess.DEVNULL,
            ).strip()
            if mem_off.lstrip("-").isdigit():
                self.spin_mem.setValue(int(mem_off))
        except Exception:
            pass

        # Refresh the system-info tab so it reflects the current GPU
        self.refresh_system_info()

    # ------------------------------------------------------------------ #
    #  sudo credential management                                         #
    # ------------------------------------------------------------------ #
    def _refresh_sudo(self):
        """Probe the credential cache non-interactively and update the status label."""
        result = subprocess.run(["sudo", "-n", "-v"], capture_output=True)
        self._sudo_ok = (result.returncode == 0)
        self._set_sudo_badge(self._sudo_ok)

    def _set_sudo_badge(self, ok: bool):
        if not hasattr(self, "_sudo_status_lbl"):
            return
        if ok:
            self._sudo_status_lbl.setText("● sudo: active")
            self._sudo_status_lbl.setStyleSheet(
                "color: #9ece6a; font-size: 13px; font-weight: bold;"
                " background-color: #1e2a1e; padding: 4px 14px;"
                " border-radius: 6px; border: 1px solid #9ece6a;"
            )
        else:
            self._sudo_status_lbl.setText("● sudo: EXPIRED — fan control paused")
            self._sudo_status_lbl.setStyleSheet(
                "color: #f7768e; font-size: 13px; font-weight: bold;"
                " background-color: #2a1e1e; padding: 4px 14px;"
                " border-radius: 6px; border: 1px solid #f7768e;"
            )

    def _handle_sudo_failure(self):
        """Called when a sudo nvidia-settings command fails. Pauses fan control."""
        self._sudo_ok = False
        self._set_sudo_badge(False)
        if not self._sudo_warned:
            self._sudo_warned = True
            QMessageBox.warning(
                self, "sudo credentials expired",
                "Fan control requires sudo credentials, which have expired.\n\n"
                "The fan curve has been paused at its last applied speed.\n"
                "Control will resume automatically on the next 4-minute refresh.\n\n"
                "To refresh immediately, run in a terminal:\n"
                "    sudo -v",
            )

    def _restore_auto_fan(self):
        """Restore VBIOS automatic fan control. Best-effort — does not crash on failure."""
        idx = self.current_gpu_index
        subprocess.run(
            ["sudo", "-n", "nvidia-settings", "-a", f"[gpu:{idx}]/GPUFanControlState=0"],
            capture_output=True,
        )
        self.last_applied_fan = -1

    def _get_fan_start_idx(self):
        """Return the global nvidia-settings fan index for the first fan of the current GPU."""
        start = 0
        for j in range(self.current_gpu_index):
            try:
                start += pynvml.nvmlDeviceGetNumFans(pynvml.nvmlDeviceGetHandleByIndex(j))
            except Exception:
                pass
        return start

    def closeEvent(self, event):
        """Restore auto fan control and shut down NVML cleanly on exit."""
        if self.fan_mode.currentText() != "VBIOS Auto":
            self._restore_auto_fan()
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass
        event.accept()

    def initUI(self):
        self.setWindowTitle("NVIDIA Pro Tuner")
        self.resize(1000, 950)
        self.setStyleSheet("QWidget { background-color: #1a1b26; color: #a9b1d6; }")
        layout = QVBoxLayout(self)

        # Top bar: GPU selector + sudo status badge side by side
        top_bar = QHBoxLayout()

        self.gpu_selector = QComboBox()
        for i in range(self.device_count):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(h)
            if isinstance(name, bytes):
                name = name.decode()
            self.gpu_selector.addItem(f"GPU {i}: {name}")
        self.gpu_selector.currentIndexChanged.connect(self.change_gpu)
        top_bar.addWidget(self.gpu_selector, stretch=1)

        self._sudo_status_lbl = QLabel("● sudo: active")
        self._sudo_status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sudo_status_lbl.setStyleSheet(
            "color: #9ece6a; font-size: 13px; font-weight: bold;"
            " background-color: #1e2a1e; padding: 4px 14px;"
            " border-radius: 6px; border: 1px solid #9ece6a;"
        )
        self._sudo_status_lbl.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        top_bar.addWidget(self._sudo_status_lbl)

        layout.addLayout(top_bar)

        self.tabs = QTabWidget()
        self.setup_tuning_tab()
        self.setup_monitor_tab()
        self.setup_fan_curve_tab()
        self.setup_info_tab()
        layout.addWidget(self.tabs)

        self.refresh_gpu_limits()

    # ------------------------------------------------------------------ #
    #  Monitor tab                                                         #
    # ------------------------------------------------------------------ #
    def setup_monitor_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)

        self.mon_labels = {
            k: QLabel(f"{k.capitalize()}: --")
            for k in ['temp', 'core', 'mem', 'pwr', 'fan']
        }
        self.throttle_lbl = QLabel("Throttle: None")
        self.throttle_lbl.setStyleSheet("color: #9ece6a;")

        grid = QGridLayout()
        for i, (k, lbl) in enumerate(self.mon_labels.items()):
            grid.addWidget(lbl, i // 2, i % 2)
        grid.addWidget(self.throttle_lbl, len(self.mon_labels) // 2 + 1, 0, 1, 2)
        lay.addLayout(grid)

        self.gpu_graph = LiveGraph("GPU Utilization", QColor(122, 162, 247))
        self.vram_graph = LiveGraph("VRAM Utilization", QColor(158, 206, 106))
        self.core_clk_graph = LiveGraph(
            "Core Clock",
            QColor(224, 175, 104),
            max_val=self.max_core_clk,
            unit=" MHz",
            ref_line=self.max_core_clk,
        )
        self.mem_clk_graph = LiveGraph(
            "Mem Clock",
            QColor(187, 154, 247),
            max_val=self.max_mem_clk,
            unit=" MHz",
            ref_line=self.max_mem_clk,
        )
        self.fan_graph = LiveGraph("Fan Speed", QColor(247, 118, 142))

        for g in (self.gpu_graph, self.vram_graph, self.core_clk_graph,
                  self.mem_clk_graph, self.fan_graph):
            lay.addWidget(g)

        self.tabs.addTab(tab, "Monitor")

    # ------------------------------------------------------------------ #
    #  Fan Curve tab                                                       #
    # ------------------------------------------------------------------ #
    def setup_fan_curve_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)

        self.fan_mode = QComboBox()
        self.fan_mode.addItems(["VBIOS Auto", "Manual Fixed", "Software Curve"])
        self.fan_mode.currentIndexChanged.connect(self.on_fan_mode_changed)

        lay.addWidget(QLabel("Fan Mode:"))
        lay.addWidget(self.fan_mode)

        self.manual_fan_container = QGroupBox("Manual Speed %")
        m_lay = QHBoxLayout()
        self.slider_fan = QSlider(Qt.Orientation.Horizontal)
        self.slider_fan.setRange(20, 100)
        self.slider_fan.setValue(50)
        self.lbl_slider = QLabel("50%")
        self.slider_fan.valueChanged.connect(lambda v: self.lbl_slider.setText(f"{v}%"))
        m_lay.addWidget(self.slider_fan)
        m_lay.addWidget(self.lbl_slider)
        self.manual_fan_container.setLayout(m_lay)
        lay.addWidget(self.manual_fan_container)

        self.curve_container = QWidget()
        c_lay = QHBoxLayout(self.curve_container)
        self.graph = FanCurveGraph()
        self.graph.curveChanged.connect(self.update_table_from_graph)
        self.table = QTableWidget(len(self.graph.points), 2)
        self.table.setHorizontalHeaderLabels(["Temp °C", "Fan %"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self.update_graph_from_table)
        c_lay.addWidget(self.graph, 2)
        c_lay.addWidget(self.table, 1)
        lay.addWidget(self.curve_container)

        # Populate table immediately so values are visible on first open
        self.update_table_from_graph(self.graph.points)

        self.toggle_fan_ui()
        self.tabs.addTab(tab, "Fan Curve")

    def on_fan_mode_changed(self):
        self.toggle_fan_ui()
        if self.fan_mode.currentText() == "VBIOS Auto":
            self._restore_auto_fan()

    def toggle_fan_ui(self):
        m = self.fan_mode.currentText()
        self.manual_fan_container.setVisible(m == "Manual Fixed")
        self.curve_container.setVisible(m == "Software Curve")

    def update_table_from_graph(self, points):
        self.table.blockSignals(True)
        self.table.setRowCount(len(points))
        for i, (t, s) in enumerate(points):
            self.table.setItem(i, 0, QTableWidgetItem(str(t)))
            self.table.setItem(i, 1, QTableWidgetItem(str(s)))
        self.table.blockSignals(False)

    def update_graph_from_table(self):
        try:
            pts = []
            for i in range(self.table.rowCount()):
                t = int(self.table.item(i, 0).text())
                s = int(self.table.item(i, 1).text())
                if i > 0:
                    if t <= pts[i - 1][0]:
                        t = pts[i - 1][0] + 1
                    if s < pts[i - 1][1]:
                        s = pts[i - 1][1]
                pts.append([t, s])
            self.update_table_from_graph(pts)
            self.graph.points = pts
            self.graph.update()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    #  Overclocking tab                                                    #
    # ------------------------------------------------------------------ #
    def setup_tuning_tab(self):
        tab = QWidget()
        lay = QGridLayout(tab)
        self.lbl_pwr_range = QLabel("Range: --")
        self.spin_pwr = QSpinBox()
        self.spin_pwr.setRange(50, 600)   # will be tightened by _load_gpu_defaults

        lay.addWidget(QLabel("Power Limit (W):"), 0, 0)
        lay.addWidget(self.spin_pwr, 0, 1)
        lay.addWidget(self.lbl_pwr_range, 0, 2)

        self.spin_core = QSpinBox()
        self.spin_core.setRange(-500, 1000)
        lay.addWidget(QLabel("Core Offset:"), 1, 0)
        lay.addWidget(self.spin_core, 1, 1)

        self.spin_mem = QSpinBox()
        self.spin_mem.setRange(-1000, 2000)
        lay.addWidget(QLabel("Mem Offset:"), 2, 0)
        lay.addWidget(self.spin_mem, 2, 1)

        btn = QPushButton("Apply Tuning")
        btn.clicked.connect(self.apply_tuning)
        lay.addWidget(btn, 3, 0, 1, 3)
        self.tabs.addTab(tab, "Overclocking")

    def refresh_gpu_limits(self):
        try:
            p_min, p_max = pynvml.nvmlDeviceGetPowerManagementLimitConstraints(self.handle)
            self.spin_pwr.setRange(p_min // 1000, p_max // 1000)
            self.lbl_pwr_range.setText(f"{p_min // 1000}W – {p_max // 1000}W")
        except Exception:
            try:
                curr = pynvml.nvmlDeviceGetPowerManagementLimit(self.handle) // 1000
                self.lbl_pwr_range.setText(f"Current Limit: {curr}W")
            except Exception:
                self.lbl_pwr_range.setText("Range: N/A (Run as Root?)")

    # ------------------------------------------------------------------ #
    #  Update loop (1 Hz)                                                  #
    # ------------------------------------------------------------------ #
    def update_loop(self):
        try:
            h = self.handle
            temp     = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
            pwr      = pynvml.nvmlDeviceGetPowerUsage(h) / 1000
            core     = pynvml.nvmlDeviceGetClockInfo(h, pynvml.NVML_CLOCK_GRAPHICS)
            mem_clk  = pynvml.nvmlDeviceGetClockInfo(h, pynvml.NVML_CLOCK_MEM)
            util     = pynvml.nvmlDeviceGetUtilizationRates(h)
            mem_info = pynvml.nvmlDeviceGetMemoryInfo(h)

            vram_mb  = mem_info.used / (1024 ** 2)
            vram_pct = int((mem_info.used / mem_info.total) * 100)

            fan_pct = None
            fan_rpm_display = ""
            try:
                fan_pct = pynvml.nvmlDeviceGetFanSpeed(h)
                try:
                    idx = self.current_gpu_index
                    rpm_out = subprocess.check_output(
                        f"nvidia-settings -t -q '[fan:{idx}]/GPUCurrentFanSpeedRPM'",
                        shell=True, text=True, stderr=subprocess.DEVNULL,
                    ).strip()
                    if rpm_out.isdigit():
                        fan_rpm_display = f" | {rpm_out} RPM"
                except Exception:
                    pass
            except Exception:
                pass

            fan_display = (
                f"{fan_pct}%{fan_rpm_display}" if fan_pct is not None else "N/A"
            )

            # Throttle reason
            # nvidia-ml-py 12+ renamed the function; fall back to old name for compat.
            throttle_text = "None"
            throttle_color = "#9ece6a"
            try:
                try:
                    reasons = pynvml.nvmlDeviceGetCurrentClocksEventReasons(h)
                except AttributeError:
                    reasons = pynvml.nvmlDeviceGetCurrentClocksThrottleReasons(h)
                active = [lbl for bit, lbl in THROTTLE_REASONS.items() if reasons & bit]
                if active:
                    throttle_text = ", ".join(active)
                    throttle_color = "#f7768e"
            except Exception:
                throttle_text = "N/A"
                throttle_color = "#a9b1d6"

            self.mon_labels['temp'].setText(f"Temp: {temp}°C")
            self.mon_labels['core'].setText(f"Core: {core} MHz")
            self.mon_labels['mem'].setText(f"Mem: {mem_clk} MHz")
            self.mon_labels['pwr'].setText(f"Power: {pwr:.1f}W")
            self.mon_labels['fan'].setText(f"Fan Speed: {fan_display}")
            self.throttle_lbl.setText(f"Throttle: {throttle_text}")
            self.throttle_lbl.setStyleSheet(f"color: {throttle_color};")

            self.gpu_graph.update_data(util.gpu, f"{util.gpu}%")
            self.vram_graph.update_data(vram_pct, f"{vram_pct}% ({vram_mb:.0f} MB)")
            self.core_clk_graph.update_data(core, f"{core} MHz")
            self.mem_clk_graph.update_data(mem_clk, f"{mem_clk} MHz")
            self.fan_graph.update_data(
                fan_pct if fan_pct is not None else 0,
                fan_display,
            )

            # Hardware fan control dispatch
            mode = self.fan_mode.currentText()
            if mode == "Software Curve":
                self.apply_all_fans(self.calculate_target_fan(temp))
            elif mode == "Manual Fixed":
                self.apply_all_fans(self.slider_fan.value())

        except Exception as e:
            print(f"Loop error: {e}")

    def calculate_target_fan(self, temp):
        """
        Linearly interpolate between set-points so fan speed changes smoothly
        as temperature moves between them instead of jumping in discrete steps.
        """
        pts = sorted(self.graph.points)   # ensure ascending temperature order

        # Below first point: return first point's fan speed
        if temp <= pts[0][0]:
            return pts[0][1]

        # Above last point: return last point's fan speed
        if temp >= pts[-1][0]:
            return pts[-1][1]

        # Find the surrounding segment and interpolate
        for i in range(len(pts) - 1):
            t0, s0 = pts[i]
            t1, s1 = pts[i + 1]
            if t0 <= temp <= t1:
                if t1 == t0:          # degenerate segment (same temp), avoid div-by-zero
                    return s1
                frac = (temp - t0) / (t1 - t0)
                return int(round(s0 + frac * (s1 - s0)))

        return pts[-1][1]   # fallback (should be unreachable)

    def apply_all_fans(self, speed):
        if speed == self.last_applied_fan:
            return
        if not self._sudo_ok:
            return  # warning already shown; wait for next _refresh_sudo

        idx = self.current_gpu_index

        # Enable manual fan control — if this fails, sudo has expired.
        r = subprocess.run(
            ["sudo", "-n", "nvidia-settings", "-a", f"[gpu:{idx}]/GPUFanControlState=1"],
            capture_output=True,
        )
        if r.returncode != 0:
            self._handle_sudo_failure()
            return

        try:
            num_fans = pynvml.nvmlDeviceGetNumFans(self.handle)
        except Exception:
            num_fans = 4

        # nvidia-settings uses global fan indices across all GPUs.
        fan_start = self._get_fan_start_idx()
        for fi in range(num_fans):
            subprocess.run(
                ["sudo", "-n", "nvidia-settings", "-a",
                 f"[fan:{fan_start + fi}]/GPUTargetFanSpeed={speed}"],
                capture_output=True,
            )

        self.last_applied_fan = speed

    def change_gpu(self, i):
        self.current_gpu_index = i
        self.handle = pynvml.nvmlDeviceGetHandleByIndex(i)
        self.last_applied_fan = -1
        self._init_clock_limits()
        self.core_clk_graph.max_val  = self.max_core_clk
        self.core_clk_graph.ref_line = self.max_core_clk
        self.mem_clk_graph.max_val   = self.max_mem_clk
        self.mem_clk_graph.ref_line  = self.max_mem_clk
        self.refresh_gpu_limits()
        self._load_gpu_defaults()

    def apply_tuning(self):
        idx = self.current_gpu_index
        errors = []

        r = subprocess.run(
            ["sudo", "-n", "nvidia-smi", "-i", str(idx), "-pl", str(self.spin_pwr.value())],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            errors.append(f"Power limit: {r.stderr.strip() or 'failed'}")

        r = subprocess.run(
            ["sudo", "-n", "nvidia-settings", "-a",
             f"[gpu:{idx}]/GPUGraphicsClockOffsetAllPerformanceLevels={self.spin_core.value()}"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            errors.append(f"Core offset: {r.stderr.strip() or 'failed'}")

        r = subprocess.run(
            ["sudo", "-n", "nvidia-settings", "-a",
             f"[gpu:{idx}]/GPUMemoryTransferRateOffsetAllPerformanceLevels={self.spin_mem.value()}"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            errors.append(f"Mem offset: {r.stderr.strip() or 'failed'}")

        if errors:
            QMessageBox.warning(self, "Apply failed", "\n".join(errors))
        else:
            QMessageBox.information(self, "Applied", "Tuning settings applied successfully.")

    # ------------------------------------------------------------------ #
    #  System Info tab                                                     #
    # ------------------------------------------------------------------ #
    def setup_info_tab(self):
        tab = QWidget()
        outer = QVBoxLayout(tab)

        refresh_btn = QPushButton("Refresh Info")
        refresh_btn.clicked.connect(self.refresh_system_info)
        outer.addWidget(refresh_btn)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        self.info_layout = QVBoxLayout(inner)
        self.info_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.info_layout.setSpacing(8)

        # System section — rebuilt on every refresh
        self._sys_info_container = QWidget()
        self._sys_info_vbox = QVBoxLayout(self._sys_info_container)
        self._sys_info_vbox.setContentsMargins(0, 0, 0, 0)
        self._sys_info_vbox.setSpacing(6)
        self.info_layout.addWidget(self._sys_info_container)

        # GPU / NVIDIA section — existing text label
        gpu_sep = QLabel("━" * 60 + "  GPU / NVIDIA  " + "━" * 60)
        gpu_sep.setFont(QFont("monospace", 8))
        gpu_sep.setStyleSheet("color: #565f89;")
        self.info_layout.addWidget(gpu_sep)

        self.info_lbl = QLabel("Loading…")
        self.info_lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.info_lbl.setWordWrap(True)
        self.info_lbl.setFont(QFont("monospace", 9))
        self.info_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.info_layout.addWidget(self.info_lbl)

        scroll.setWidget(inner)
        outer.addWidget(scroll)
        self.tabs.addTab(tab, "System Info")

    # ------------------------------------------------------------------ #
    #  System info helpers                                                 #
    # ------------------------------------------------------------------ #
    _GROUP_STYLE = (
        "QGroupBox { font-weight: bold; font-size: 11px;"
        " border: 1px solid #3b4261; border-radius: 5px;"
        " margin-top: 10px; padding-top: 4px; }"
        "QGroupBox::title { subcontrol-origin: margin; left: 10px;"
        " padding: 0 4px; color: #7aa2f7; }"
    )

    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
            sub = item.layout()
            if sub is not None:
                self._clear_layout(sub)

    def _make_info_row(self, key, value):
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 1, 0, 1)
        k = QLabel(f"{key}:")
        k.setFont(QFont("monospace", 9))
        k.setFixedWidth(170)
        k.setStyleSheet("color: #7aa2f7;")
        h.addWidget(k)
        v = QLabel(str(value))
        v.setFont(QFont("monospace", 9))
        v.setWordWrap(True)
        v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        h.addWidget(v, stretch=1)
        return row

    def _make_reveal_row(self, key, real_value):
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 1, 0, 1)
        k = QLabel(f"{key}:")
        k.setFont(QFont("monospace", 9))
        k.setFixedWidth(170)
        k.setStyleSheet("color: #7aa2f7;")
        h.addWidget(k)
        v = QLabel("● ● ● ● ● ● ● ● ● ● ● ●  [hidden]")
        v.setFont(QFont("monospace", 9))
        v.setStyleSheet("color: #565f89;")
        v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        h.addWidget(v, stretch=1)
        btn = QPushButton("Show")
        btn.setFixedWidth(55)
        btn.setCheckable(True)
        btn.setStyleSheet(
            "QPushButton { background:#1f2335; border:1px solid #3b4261;"
            " border-radius:3px; padding:2px 6px; font-size:10px; }"
            "QPushButton:checked { background:#2d3f5e; border-color:#7aa2f7; color:#7aa2f7; }"
        )
        def _toggle(checked, lbl=v, real=real_value, b=btn):
            if checked:
                lbl.setText(real)
                lbl.setStyleSheet("color: #a9b1d6;")
                b.setText("Hide")
            else:
                lbl.setText("● ● ● ● ● ● ● ● ● ● ● ●  [hidden]")
                lbl.setStyleSheet("color: #565f89;")
                b.setText("Show")
        btn.toggled.connect(_toggle)
        h.addWidget(btn)
        return row

    def _add_sys_group(self, title, rows):
        if not rows:
            return
        grp = QGroupBox(title)
        grp.setStyleSheet(self._GROUP_STYLE)
        vlay = QVBoxLayout()
        vlay.setSpacing(2)
        vlay.setContentsMargins(8, 4, 8, 8)
        for r in rows:
            vlay.addWidget(r)
        grp.setLayout(vlay)
        self._sys_info_vbox.addWidget(grp)

    def refresh_system_info(self):
        self._clear_layout(self._sys_info_vbox)

        # ── System group ─────────────────────────────────────────────────
        sys_rows = []
        try:
            sys_rows.append(self._make_info_row("Kernel", subprocess.check_output(
                ["uname", "-r"], text=True).strip()))
            sys_rows.append(self._make_info_row("Kernel (full)", subprocess.check_output(
                ["uname", "-a"], text=True).strip()))
        except Exception:
            pass
        try:
            lsb = subprocess.check_output(
                ["lsb_release", "-a"], text=True, stderr=subprocess.DEVNULL)
            for line in lsb.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    if k.strip() in ("Distributor ID", "Description", "Release", "Codename"):
                        sys_rows.append(self._make_info_row(k.strip(), v.strip()))
        except Exception:
            pass
        try:
            gnome = subprocess.check_output(
                ["gnome-shell", "--version"], text=True, stderr=subprocess.DEVNULL).strip()
            sys_rows.append(self._make_info_row("GNOME Shell", gnome))
        except Exception:
            pass
        try:
            hc = subprocess.check_output(["hostnamectl"], text=True, stderr=subprocess.DEVNULL)
            _WANT = {
                "Static hostname": "Hostname",
                "Operating System": "OS",
                "Hardware Vendor":  "HW Vendor",
                "Hardware Model":   "HW Model",
                "Firmware Version": "Firmware",
                "Firmware Date":    "Firmware Date",
                "Firmware Age":     "Firmware Age",
            }
            _HIDDEN = {"Machine ID": "Machine ID", "Boot ID": "Boot ID"}
            for line in hc.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    k_s, v_s = k.strip(), v.strip()
                    if k_s in _WANT:
                        sys_rows.append(self._make_info_row(_WANT[k_s], v_s))
                    elif k_s in _HIDDEN:
                        sys_rows.append(self._make_reveal_row(_HIDDEN[k_s], v_s))
        except Exception:
            pass
        self._add_sys_group("System", sys_rows)

        # ── CPU group ────────────────────────────────────────────────────
        cpu_rows = []
        try:
            lscpu = subprocess.check_output(["lscpu"], text=True, stderr=subprocess.DEVNULL)
            _WANT_CPU = {
                "Model name", "Architecture", "Byte Order",
                "CPU(s)", "Thread(s) per core", "Core(s) per socket", "Socket(s)",
                "CPU max MHz", "CPU min MHz", "BogoMIPS",
                "Virtualization",
                "L1d cache", "L1i cache", "L2 cache", "L3 cache",
            }
            for line in lscpu.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    if k.strip() in _WANT_CPU:
                        cpu_rows.append(self._make_info_row(k.strip(), v.strip()))
        except Exception:
            pass
        self._add_sys_group("CPU", cpu_rows)

        # ── Motherboard group (sysfs DMI — no sudo needed) ───────────────
        mb_rows = []
        def _dmi(path):
            try:
                return open(path).read().strip()
            except Exception:
                return None
        for label, path in [
            ("Board Vendor",    "/sys/class/dmi/id/board_vendor"),
            ("Board Name",      "/sys/class/dmi/id/board_name"),
            ("Board Version",   "/sys/class/dmi/id/board_version"),
            ("System Vendor",   "/sys/class/dmi/id/sys_vendor"),
            ("Product Name",    "/sys/class/dmi/id/product_name"),
            ("Product Version", "/sys/class/dmi/id/product_version"),
            ("BIOS Vendor",     "/sys/class/dmi/id/bios_vendor"),
            ("BIOS Version",    "/sys/class/dmi/id/bios_version"),
            ("BIOS Date",       "/sys/class/dmi/id/bios_date"),
        ]:
            val = _dmi(path)
            if val:
                mb_rows.append(self._make_info_row(label, val))
        self._add_sys_group("Motherboard", mb_rows)

        # ── Memory group ─────────────────────────────────────────────────
        mem_rows = []
        try:
            free = subprocess.check_output(["free", "-h", "--si"], text=True)
            header = free.splitlines()[0].split()
            for line in free.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 2:
                    label = parts[0].rstrip(":")
                    for col, val in zip(header, parts[1:]):
                        mem_rows.append(self._make_info_row(f"{label} {col}", val))
        except Exception:
            try:
                for line in open("/proc/meminfo"):
                    if line.startswith(("MemTotal", "MemAvailable", "SwapTotal", "SwapFree")):
                        k, v = line.split(":", 1)
                        mem_rows.append(self._make_info_row(k.strip(), v.strip()))
            except Exception:
                pass
        # Memory slot details via dmidecode (requires sudo)
        try:
            dmi_mem = subprocess.check_output(
                ["sudo", "-n", "dmidecode", "-t", "memory"],
                text=True, stderr=subprocess.DEVNULL,
            )
            _SLOT_FIELDS = {"Size", "Type", "Speed", "Configured Memory Speed",
                            "Manufacturer", "Part Number", "Locator"}
            slot, slot_n = {}, 0
            for line in dmi_mem.splitlines():
                s = line.strip()
                if s.startswith("Memory Device"):
                    if slot and slot.get("Size", "No Module Installed") != "No Module Installed":
                        mem_rows.append(self._make_info_row(f"── Slot {slot_n}", ""))
                        for f in ("Locator", "Size", "Type", "Speed",
                                  "Configured Memory Speed", "Manufacturer", "Part Number"):
                            if f in slot:
                                mem_rows.append(self._make_info_row(f"   {f}", slot[f]))
                        slot_n += 1
                    slot = {}
                elif ":" in s:
                    k, v = s.split(":", 1)
                    if k.strip() in _SLOT_FIELDS:
                        slot[k.strip()] = v.strip()
            if slot and slot.get("Size", "No Module Installed") != "No Module Installed":
                mem_rows.append(self._make_info_row(f"── Slot {slot_n}", ""))
                for f in ("Locator", "Size", "Type", "Speed",
                          "Configured Memory Speed", "Manufacturer", "Part Number"):
                    if f in slot:
                        mem_rows.append(self._make_info_row(f"   {f}", slot[f]))
        except Exception:
            pass
        self._add_sys_group("Memory", mem_rows)

        # ── GPU / NVIDIA (existing) ───────────────────────────────────────
        lines = []
        h = self.handle
        idx = self.current_gpu_index

        # ── Basic NVML info ──────────────────────────────────────────────
        try:
            name = pynvml.nvmlDeviceGetName(h)
            if isinstance(name, bytes):
                name = name.decode()
            lines.append(f"GPU:            {name}")
            lines.append(f"Driver:         {pynvml.nvmlSystemGetDriverVersion()}")
            c = pynvml.nvmlSystemGetCudaDriverVersion()
            lines.append(f"CUDA:           {c // 1000}.{(c % 1000) // 10}")
        except Exception as e:
            lines.append(f"[NVML basic error: {e}]")

        # ── Fan info ─────────────────────────────────────────────────────
        try:
            num_fans = pynvml.nvmlDeviceGetNumFans(h)
            lines.append(f"Fans:           {num_fans}")
            for fi in range(num_fans):
                try:
                    spd = pynvml.nvmlDeviceGetFanSpeed_v2(h, fi)
                    lines.append(f"  Fan {fi} speed:  {spd}%")
                except Exception:
                    pass
        except Exception:
            lines.append("Fans:           N/A")

        # ── Core clock (min / current / max) ────────────────────────────
        try:
            core_cur  = pynvml.nvmlDeviceGetClockInfo(h, pynvml.NVML_CLOCK_GRAPHICS)
            core_max  = pynvml.nvmlDeviceGetMaxClockInfo(h, pynvml.NVML_CLOCK_GRAPHICS)
            # Minimum = lowest supported graphics clock (last entry in the list)
            try:
                supp = pynvml.nvmlDeviceGetSupportedGraphicsClocks(h, 0)
                core_min = min(supp) if supp else "N/A"
            except Exception:
                core_min = "N/A"
            lines.append(f"Core clock:     min={core_min} MHz  cur={core_cur} MHz  max={core_max} MHz")
        except Exception as e:
            lines.append(f"Core clock:     N/A ({e})")

        # ── Memory clock (min / current / max) ──────────────────────────
        try:
            mem_cur  = pynvml.nvmlDeviceGetClockInfo(h, pynvml.NVML_CLOCK_MEM)
            mem_max  = pynvml.nvmlDeviceGetMaxClockInfo(h, pynvml.NVML_CLOCK_MEM)
            try:
                supp_mem = pynvml.nvmlDeviceGetSupportedMemoryClocks(h)
                mem_min  = min(supp_mem) if supp_mem else "N/A"
            except Exception:
                mem_min = "N/A"
            lines.append(f"Mem clock:      min={mem_min} MHz  cur={mem_cur} MHz  max={mem_max} MHz")
        except Exception as e:
            lines.append(f"Mem clock:      N/A ({e})")

        # ── Power ────────────────────────────────────────────────────────
        try:
            pl_cur   = pynvml.nvmlDeviceGetPowerManagementLimit(h) // 1000
            pl_def   = pynvml.nvmlDeviceGetPowerManagementDefaultLimit(h) // 1000
            p_min, p_max = pynvml.nvmlDeviceGetPowerManagementLimitConstraints(h)
            lines.append(
                f"Power limit:    {pl_cur}W  (default={pl_def}W,  range={p_min//1000}–{p_max//1000}W)"
            )
        except Exception as e:
            lines.append(f"Power limit:    N/A ({e})")

        # ── Memory ───────────────────────────────────────────────────────
        try:
            mi = pynvml.nvmlDeviceGetMemoryInfo(h)
            lines.append(
                f"VRAM:           {mi.used/(1024**2):.0f} MB used / "
                f"{mi.total/(1024**2):.0f} MB total"
            )
        except Exception:
            pass

        # ── Temperature thresholds ───────────────────────────────────────
        TEMP_TYPES = {
            "Shutdown":  pynvml.NVML_TEMPERATURE_THRESHOLD_SHUTDOWN,
            "Slowdown":  pynvml.NVML_TEMPERATURE_THRESHOLD_SLOWDOWN,
        }
        for name, ttype in TEMP_TYPES.items():
            try:
                val = pynvml.nvmlDeviceGetTemperatureThreshold(h, ttype)
                lines.append(f"Temp {name:9s}: {val}°C")
            except Exception:
                pass

        # ── PCIe ─────────────────────────────────────────────────────────
        try:
            bus = pynvml.nvmlDeviceGetPciInfo(h)
            bus_id = bus.busId
            if isinstance(bus_id, bytes):
                bus_id = bus_id.decode()
            lines.append(f"PCIe bus ID:    {bus_id}")
        except Exception:
            pass
        try:
            link_gen   = pynvml.nvmlDeviceGetCurrPcieLinkGeneration(h)
            link_width = pynvml.nvmlDeviceGetCurrPcieLinkWidth(h)
            lines.append(f"PCIe link:      Gen{link_gen} x{link_width}")
        except Exception:
            pass

        lines.append("")
        lines.append("─" * 60)
        lines.append("nvidia-smi -q output (key fields):")
        lines.append("─" * 60)

        # ── nvidia-smi -q (rich structured data) ────────────────────────
        SMI_SECTIONS = [
            "Product Name",
            "Product Brand",
            "Display Mode",
            "Persistence Mode",
            "Accounting Mode",
            "GPU UUID",
            "Minor Number",
            "Serial Number",
            "Board Part Number",
            "Inforom Version",
            "GPU Operation Mode",
            "MIG Mode",
            "Power Draw",
            "Power Limit",
            "Default Power Limit",
            "Min Power Limit",
            "Max Power Limit",
            "Clocks Throttle Reasons",
            "Graphics",   # current clocks section
            "Memory",
            "SM",
            "Video",
            "Applications Clocks",
            "Default Applications Clocks",
            "Max Clocks",
            "Max Customer Boost Clocks",
            "Clock Policy",
            "Fan Speed",
            "Performance State",
            "Compute Mode",
            "Utilization",
            "Encoder Stats",
            "FBC Stats",
            "ECC Errors",
            "Retired Pages",
            "Temperature",
            "Voltage",
            "Fabric",
        ]

        try:
            smi_out = subprocess.check_output(
                f"nvidia-smi -q -i {idx}",
                shell=True, text=True, stderr=subprocess.DEVNULL,
            )
            # Include every line; trim excess whitespace but keep structure
            for raw_line in smi_out.splitlines():
                stripped = raw_line.strip()
                if stripped:
                    lines.append(stripped)
        except Exception as e:
            lines.append(f"nvidia-smi -q failed: {e}")
            # Fallback: try the common query fields individually
            try:
                fields = (
                    "name,driver_version,vbios_version,pstate,fan.speed,"
                    "temperature.gpu,power.draw,power.limit,power.default_limit,"
                    "power.min_limit,power.max_limit,"
                    "clocks.gr,clocks.mem,clocks.sm,clocks.video,"
                    "clocks.max.gr,clocks.max.mem,clocks.max.sm,"
                    "utilization.gpu,utilization.memory,"
                    "memory.total,memory.free,memory.used,"
                    "ecc.mode.current,ecc.errors.uncorrected.volatile.total,"
                    "retired_pages.single_bit_ecc.count,"
                    "retired_pages.double_bit.count,"
                    "compute_mode,persistence_mode,accounting.mode"
                )
                csv_out = subprocess.check_output(
                    f"nvidia-smi --query-gpu={fields} --format=csv -i {idx}",
                    shell=True, text=True, stderr=subprocess.DEVNULL,
                )
                lines.append(csv_out)
            except Exception as e2:
                lines.append(f"Fallback query also failed: {e2}")

        self.info_lbl.setText("\n".join(lines))


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = AdvancedRTXTuner()
    win.show()
    sys.exit(app.exec())
