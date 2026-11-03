"""SpecView - browse the individual averages of an SVS series, pick some, save their mean."""
import os
import sys
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QGuiApplication, QIcon, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QProgressDialog, QPushButton, QScrollArea, QSplitter,
    QSizePolicy, QTabWidget, QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

import mrs_io

APP_NAME = "SpecView"
MODES = ["Magnitude", "Real (auto-phased)", "Imaginary (auto-phased)"]
THEMES = {"System": Qt.ColorScheme.Unknown, "Light": Qt.ColorScheme.Light, "Dark": Qt.ColorScheme.Dark}
CORR_WARN = 0.95  # flag averages whose magnitude spectrum correlates less than this with the median


class SaveDialog(QDialog):
    def __init__(self, parent, default_dir, default_desc, summary):
        super().__init__(parent)
        self.setWindowTitle("Save average as DICOM")
        self.setMinimumWidth(560)
        self.dir_edit = QLineEdit(str(default_dir))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse)
        row = QHBoxLayout()
        row.addWidget(self.dir_edit, 1)
        row.addWidget(browse)
        self.desc_edit = QLineEdit(default_desc)
        self.desc_edit.setMaxLength(64)
        form = QFormLayout(self)
        form.addRow(QLabel(summary))
        form.addRow("Output folder", row)
        form.addRow("Series description", self.desc_edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def browse(self):
        d = QFileDialog.getExistingDirectory(self, "Output folder", self.dir_edit.text())
        if d:
            self.dir_edit.setText(d)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings(APP_NAME, APP_NAME)
        self.folder = None
        self.series = []
        self.spectra = []
        self.included = []
        self.stats = []
        self.current = 0
        self.grid_plots = []
        self.grid_curves = []

        self.setWindowTitle(APP_NAME)
        self.resize(1400, 860)
        self._build_toolbar()
        self._build_central()
        self.statusBar().showMessage("Open a patient folder to begin (Ctrl+O)")
        self.apply_plot_theme()

    # ---------- UI construction ----------
    def _build_toolbar(self):
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.addToolBar(tb)

        open_act = QAction("Open folder…", self, triggered=self.choose_folder)
        open_act.setShortcut(QKeySequence.Open)
        tb.addAction(open_act)
        tb.addWidget(QLabel("  Series "))
        self.series_combo = QComboBox()
        self.series_combo.setMinimumWidth(260)
        self.series_combo.currentIndexChanged.connect(self.set_series)
        tb.addWidget(self.series_combo)
        tb.addSeparator()

        tb.addAction(QAction("Select all", self, triggered=lambda: self.set_all(True)))
        tb.addAction(QAction("Select none", self, triggered=lambda: self.set_all(False)))
        tb.addAction(QAction("Invert", self, triggered=self.invert_selection))
        tb.addAction(QAction("Exclude flagged", self, triggered=self.exclude_flagged,
                             toolTip=f"Untick averages with correlation to the median < {CORR_WARN}"))
        tb.addSeparator()
        self.save_act = QAction("Save average…", self, triggered=self.save_average, enabled=False)
        self.save_act.setShortcut(QKeySequence.Save)
        tb.addAction(self.save_act)

        self.addToolBarBreak()
        tb = QToolBar("View")
        tb.setMovable(False)
        self.addToolBar(tb)
        tb.addWidget(QLabel("  Display "))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(MODES)
        self.mode_combo.setCurrentIndex(int(self.settings.value("mode", 0)))
        tb.addWidget(self.mode_combo)
        tb.addWidget(QLabel("  LB (Hz) "))
        self.lb_spin = self._spin(0, 50, float(self.settings.value("lb", 0)))
        self.lb_spin.setToolTip("Exponential line broadening, display only (not applied to the saved average)")
        tb.addWidget(self.lb_spin)
        tb.addWidget(QLabel("  ppm "))
        self.ppm_hi = self._spin(-20, 30, float(self.settings.value("ppm_hi", 8)))
        self.ppm_lo = self._spin(-20, 30, float(self.settings.value("ppm_lo", -1)))
        tb.addWidget(self.ppm_hi)
        tb.addWidget(QLabel(" to "))
        tb.addWidget(self.ppm_lo)
        self.mode_combo.currentIndexChanged.connect(self.refresh_all)
        for w in (self.lb_spin, self.ppm_hi, self.ppm_lo):
            w.valueChanged.connect(self.refresh_all)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        tb.addWidget(QLabel("Theme "))
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(THEMES)
        self.theme_combo.setCurrentText(self.settings.value("theme", "System"))
        self.theme_combo.currentTextChanged.connect(self.set_theme)
        tb.addWidget(self.theme_combo)

    @staticmethod
    def _spin(lo, hi, value):
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(1)
        s.setValue(value)
        return s

    def _build_central(self):
        self.table = QTreeWidget()
        self.table.setHeaderLabels(["Avg", "Peak ppm", "Height", "SNR", "Δf Hz", "Corr"])
        self.table.setRootIsDecorated(False)
        self.table.setUniformRowHeights(True)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.header().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.itemChanged.connect(self.on_item_changed)
        self.table.currentItemChanged.connect(self.on_current_item)
        self.table.setToolTip("Tick = include in the average. Corr = similarity to the median spectrum.")

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_grid_tab(), "All spectra")
        self.tabs.addTab(self._build_single_tab(), "Individual")
        self.tabs.addTab(self._build_average_tab(), "Average preview")
        self.tabs.setCurrentIndex(int(self.settings.value("tab", 0)))
        self.tabs.currentChanged.connect(lambda _: self.refresh_all())

        split = QSplitter()
        split.addWidget(self.table)
        split.addWidget(self.tabs)
        split.setStretchFactor(1, 1)
        split.setSizes([360, 1040])
        self.setCentralWidget(split)

        for key, step in ((Qt.Key_Left, -1), (Qt.Key_Right, 1)):
            sc = QShortcut(QKeySequence(key), self.tabs)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(lambda s=step: self.step(s))
        sc = QShortcut(QKeySequence(Qt.Key_Space), self.tabs)
        sc.setContext(Qt.WidgetWithChildrenShortcut)
        sc.activated.connect(lambda: self.toggle(self.current))

    def _build_grid_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        top.addWidget(QLabel("Click a spectrum to inspect it, double-click to include/exclude it."))
        top.addStretch()
        top.addWidget(QLabel("Columns"))
        self.cols_combo = QComboBox()
        self.cols_combo.addItems(["2", "3", "4", "6", "8"])
        self.cols_combo.setCurrentText(str(self.settings.value("cols", "4")))
        self.cols_combo.currentTextChanged.connect(lambda _: self.build_grid())
        top.addWidget(self.cols_combo)
        self.link_y = QCheckBox("Shared y-axis", checked=self.settings.value("link_y", "true") == "true")
        self.link_y.toggled.connect(lambda _: self.build_grid())
        top.addWidget(self.link_y)
        lay.addLayout(top)
        self.grid_widget = pg.GraphicsLayoutWidget()
        self.grid_widget.scene().sigMouseClicked.connect(self.on_grid_click)
        self.grid_scroll = QScrollArea(widgetResizable=True)
        self.grid_scroll.setWidget(self.grid_widget)
        lay.addWidget(self.grid_scroll)
        return w

    def _build_single_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        prev_b = QPushButton("◀ Previous")
        next_b = QPushButton("Next ▶")
        prev_b.clicked.connect(lambda: self.step(-1))
        next_b.clicked.connect(lambda: self.step(1))
        self.include_box = QCheckBox("Include in average (Space)")
        self.include_box.clicked.connect(lambda c: self.set_included(self.current, c))
        self.show_mean_box = QCheckBox("Overlay mean of selected", checked=True)
        self.show_mean_box.toggled.connect(lambda _: self.refresh_single())
        self.single_info = QLabel()
        for x in (prev_b, next_b, self.include_box, self.show_mean_box):
            top.addWidget(x)
        top.addStretch()
        top.addWidget(self.single_info)
        lay.addLayout(top)
        self.single_plot = pg.PlotWidget()
        self.single_plot.invertX(True)
        self.single_plot.setLabel("bottom", "ppm")
        self.single_plot.addLegend(offset=(-10, 10))
        self.single_plot.showGrid(x=True, y=True, alpha=0.15)
        lay.addWidget(self.single_plot)
        return w

    def _build_average_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.show_indiv_box = QCheckBox("Show selected averages", checked=True)
        self.show_indiv_box.toggled.connect(lambda _: self.refresh_average())
        self.show_excl_box = QCheckBox("Show excluded", checked=False)
        self.show_excl_box.toggled.connect(lambda _: self.refresh_average())
        self.align_box = QCheckBox(f"Align peaks to {mrs_io.WATER_PPM:.2f} ppm", checked=True)
        self.align_box.setToolTip("Frequency-shift each average so its tallest peak sits at "
                                  f"{mrs_io.WATER_PPM:.2f} ppm before averaging (preview only)")
        self.align_box.toggled.connect(lambda _: self.refresh_average())
        self.avg_info = QLabel()
        top.addWidget(self.align_box)
        top.addWidget(self.show_indiv_box)
        top.addWidget(self.show_excl_box)
        top.addStretch()
        top.addWidget(self.avg_info)
        lay.addLayout(top)
        self.avg_plot = pg.PlotWidget()
        self.avg_plot.invertX(True)
        self.avg_plot.setLabel("bottom", "ppm")
        self.avg_plot.addLegend(offset=(-10, 10))
        self.avg_plot.showGrid(x=True, y=True, alpha=0.15)
        lay.addWidget(self.avg_plot)
        return w

    # ---------- theme ----------
    def set_theme(self, name):
        QGuiApplication.styleHints().setColorScheme(THEMES[name])
        self.settings.setValue("theme", name)

    def changeEvent(self, ev):
        if ev.type() in (QEvent.PaletteChange, QEvent.ThemeChange) and hasattr(self, "avg_plot"):
            QTimer.singleShot(0, self.apply_plot_theme)
        super().changeEvent(ev)

    def apply_plot_theme(self):
        pal = self.palette()
        bg, fg = pal.color(QPalette.Base), pal.color(QPalette.Text)
        self.dark = bg.lightness() < 128
        for pw in (self.grid_widget, self.single_plot, self.avg_plot):
            pw.setBackground(bg)
        for item in self._plot_items():
            for ax in ("left", "bottom"):
                item.getAxis(ax).setPen(fg)
                item.getAxis(ax).setTextPen(fg)
            if item.legend is not None:
                item.legend.setLabelTextColor(fg)
        self.refresh_all()

    def _plot_items(self):
        return [self.single_plot.getPlotItem(), self.avg_plot.getPlotItem(), *self.grid_plots]

    def colors(self):
        pal = self.palette()
        accent = pal.color(QPalette.Accent) if hasattr(QPalette, "Accent") else pal.color(QPalette.Highlight)
        return {
            "accent": accent,
            "fg": pal.color(QPalette.Text),
            "muted": QColor(120, 120, 120) if self.dark else QColor(175, 175, 175),
            "mean": QColor(255, 170, 60) if self.dark else QColor(200, 80, 0),
            "warn": QColor(255, 190, 70) if self.dark else QColor(190, 110, 0),
        }

    # ---------- data ----------
    def choose_folder(self):
        start = self.settings.value("last_folder", str(Path.home()))
        d = QFileDialog.getExistingDirectory(self, "Open patient folder", start)
        if d:
            self.open_folder(d)

    def open_folder(self, folder):
        progress = QProgressDialog("Reading DICOMs…", None, 0, 0, self)
        progress.setWindowModality(Qt.WindowModal)
        progress.show()
        QApplication.processEvents()
        try:
            series = mrs_io.find_series(folder)
        finally:
            progress.close()
        if not series:
            QMessageBox.warning(self, APP_NAME, f"No MR spectroscopy DICOMs found in\n{folder}")
            return
        self.folder = Path(folder)
        self.settings.setValue("last_folder", str(self.folder))
        self.series = series
        self.setWindowTitle(f"{APP_NAME} — {self.folder}")
        self.series_combo.blockSignals(True)
        self.series_combo.clear()
        self.series_combo.addItems([s.label for s in series])
        self.series_combo.blockSignals(False)
        self.set_series(0)

    def set_series(self, idx):
        if idx < 0 or idx >= len(self.series):
            return
        self.spectra = self.series[idx].spectra
        self.included = [True] * len(self.spectra)
        self.stats = mrs_io.metrics(self.spectra)
        self.current = 0
        self.fill_table()
        self.build_grid()
        self.refresh_all()

    def fill_table(self):
        c = self.colors()
        self.table.blockSignals(True)
        self.table.clear()
        for i, (sp, st) in enumerate(zip(self.spectra, self.stats)):
            it = QTreeWidgetItem([f"#{sp.instance}", f"{st['peak_ppm']:.2f}", f"{st['height']:.3g}",
                                  f"{st['snr']:.0f}", f"{st['df_hz']:+.1f}", f"{st['corr']:.3f}"])
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(0, Qt.Checked)
            it.setData(0, Qt.UserRole, i)
            for col in range(1, 6):
                it.setTextAlignment(col, Qt.AlignRight | Qt.AlignVCenter)
            if st["corr"] < CORR_WARN:
                it.setForeground(5, c["warn"])
                it.setToolTip(5, "Low similarity to the median spectrum - possible motion/drift")
            self.table.addTopLevelItem(it)
        self.table.blockSignals(False)
        self.table.setCurrentItem(self.table.topLevelItem(0))

    def flagged(self):
        return [i for i, st in enumerate(self.stats) if st["corr"] < CORR_WARN]

    # ---------- selection ----------
    def set_included(self, i, on):
        if not self.spectra or self.included[i] == on:
            self.refresh_all()
            return
        self.included[i] = on
        self.table.blockSignals(True)
        self.table.topLevelItem(i).setCheckState(0, Qt.Checked if on else Qt.Unchecked)
        self.table.blockSignals(False)
        self.refresh_all()

    def toggle(self, i):
        if self.spectra:
            self.set_included(i, not self.included[i])

    def set_all(self, on):
        if not self.spectra:
            return
        self.included = [on] * len(self.spectra)
        self.sync_table_checks()

    def invert_selection(self):
        self.included = [not x for x in self.included]
        self.sync_table_checks()

    def exclude_flagged(self):
        flagged = self.flagged()
        for i in flagged:
            self.included[i] = False
        self.sync_table_checks()
        self.statusBar().showMessage(f"Excluded {len(flagged)} flagged average(s)", 5000)

    def sync_table_checks(self):
        self.table.blockSignals(True)
        for i, on in enumerate(self.included):
            self.table.topLevelItem(i).setCheckState(0, Qt.Checked if on else Qt.Unchecked)
        self.table.blockSignals(False)
        self.refresh_all()

    def on_item_changed(self, item, col):
        if col == 0:
            i = item.data(0, Qt.UserRole)
            self.included[i] = item.checkState(0) == Qt.Checked
            self.refresh_all()

    def on_current_item(self, item, _prev):
        if item is not None:
            self.set_current(item.data(0, Qt.UserRole))

    def set_current(self, i):
        if not self.spectra:
            return
        self.current = i % len(self.spectra)
        item = self.table.topLevelItem(self.current)
        if self.table.currentItem() is not item:
            self.table.setCurrentItem(item)
        self.refresh_all()

    def step(self, d):
        self.set_current(self.current + d)

    def on_grid_click(self, ev):
        pos = ev.scenePos()
        for i, p in enumerate(self.grid_plots):
            if p.sceneBoundingRect().contains(pos):
                if ev.double():
                    self.toggle(i)
                else:
                    self.set_current(i)
                return

    # ---------- rendering ----------
    def display(self, fid, sw):
        """Spectrum of fid as shown on screen (line broadening + chosen component)."""
        if self.lb_spin.value() > 0:
            t = np.arange(fid.size) / sw
            fid = fid * np.exp(-np.pi * self.lb_spin.value() * t)
        spec = np.fft.fftshift(np.fft.fft(fid))
        mode = self.mode_combo.currentIndex()
        if mode == 0:
            return np.abs(spec)
        spec = spec * np.exp(-1j * np.angle(spec[int(np.argmax(np.abs(spec)))]))  # zero-order on tallest peak
        return spec.real if mode == 1 else spec.imag

    def window(self):
        ppm = self.spectra[0].ppm
        lo, hi = sorted((self.ppm_lo.value(), self.ppm_hi.value()))
        sel = (ppm >= lo) & (ppm <= hi)
        return sel if sel.any() else np.ones_like(ppm, bool), lo, hi

    def selected(self):
        return [sp for sp, on in zip(self.spectra, self.included) if on]

    def build_grid(self):
        self.grid_widget.clear()
        self.grid_plots, self.grid_curves = [], []
        self.settings.setValue("cols", self.cols_combo.currentText())
        self.settings.setValue("link_y", str(self.link_y.isChecked()).lower())
        if not self.spectra:
            return
        ncol = int(self.cols_combo.currentText())
        nrow = int(np.ceil(len(self.spectra) / ncol))
        self.grid_widget.setMinimumHeight(nrow * (900 // max(ncol, 3)))
        for i, sp in enumerate(self.spectra):
            p = self.grid_widget.addPlot(row=i // ncol, col=i % ncol)
            p.invertX(True)
            p.hideButtons()
            p.setMenuEnabled(False)
            p.setMouseEnabled(False, False)
            p.getAxis("left").setStyle(showValues=False)
            if self.grid_plots:
                p.setXLink(self.grid_plots[0])
                if self.link_y.isChecked():
                    p.setYLink(self.grid_plots[0])
            self.grid_curves.append(p.plot())
            self.grid_plots.append(p)
        self.apply_plot_theme()

    def refresh_all(self):
        if not self.spectra:
            return
        for key, val in (("mode", self.mode_combo.currentIndex()), ("lb", self.lb_spin.value()),
                         ("ppm_hi", self.ppm_hi.value()), ("ppm_lo", self.ppm_lo.value()),
                         ("tab", self.tabs.currentIndex())):
            self.settings.setValue(key, val)
        tab = self.tabs.currentIndex()
        if tab == 0:
            self.refresh_grid()
        elif tab == 1:
            self.refresh_single()
        else:
            self.refresh_average()
        n = sum(self.included)
        self.save_act.setEnabled(n > 0)
        excl = [sp.instance for sp, on in zip(self.spectra, self.included) if not on]
        msg = f"{n} of {len(self.spectra)} averages selected"
        if excl:
            msg += f"  ·  excluded: {mrs_io.ranges(excl)}"
        if n:
            msg += f"  ·  SNR of mean ≈ {mrs_io.snr(np.fft.fftshift(np.fft.fft(mrs_io.mean_fid(self.selected())))):.0f}"
        self.statusBar().showMessage(msg)

    def refresh_grid(self):
        if not self.grid_plots:
            return
        c = self.colors()
        sel, lo, hi = self.window()
        ppm = self.spectra[0].ppm[sel]
        ymax = 0
        for i, (sp, p, curve) in enumerate(zip(self.spectra, self.grid_plots, self.grid_curves)):
            y = self.display(sp.fid, sp.sw)[sel]
            ymax = max(ymax, np.abs(y).max())
            on = self.included[i]
            curve.setData(ppm, y, pen=pg.mkPen(c["accent"] if on else c["muted"], width=1.2 if on else 0.8,
                                               style=Qt.SolidLine if on else Qt.DashLine))
            flag = " ⚠" if self.stats[i]["corr"] < CORR_WARN else ""
            title_color = (c["fg"] if on else c["muted"]).name()
            p.setTitle(f"#{sp.instance}{'' if on else ' (excluded)'}{flag}", color=title_color, size="9pt")
            p.getViewBox().setBorder(pg.mkPen(c["accent"], width=2.5) if i == self.current else None)
        self.grid_plots[0].setXRange(lo, hi, padding=0)
        if self.link_y.isChecked():
            mode0 = self.mode_combo.currentIndex() == 0
            self.grid_plots[0].setYRange(0 if mode0 else -0.3 * ymax, ymax * 1.05, padding=0)
        else:
            for p in self.grid_plots:
                p.enableAutoRange(axis="y")

    def refresh_single(self):
        c = self.colors()
        sp = self.spectra[self.current]
        sel, lo, hi = self.window()
        ppm = sp.ppm[sel]
        pw = self.single_plot
        pw.clear()
        on = self.included[self.current]
        pw.plot(ppm, self.display(sp.fid, sp.sw)[sel], name=f"#{sp.instance}",
                pen=pg.mkPen(c["accent"] if on else c["muted"], width=1.5))
        chosen = self.selected()
        if self.show_mean_box.isChecked() and chosen:
            pw.plot(ppm, self.display(mrs_io.mean_fid(chosen), sp.sw)[sel], name=f"mean of {len(chosen)}",
                    pen=pg.mkPen(c["mean"], width=1.2, style=Qt.DashLine))
        pw.setXRange(lo, hi, padding=0)
        pw.enableAutoRange(axis="y")
        pw.setTitle(f"Average #{sp.instance}  ({self.current + 1}/{len(self.spectra)})"
                    f"{'' if on else '  — excluded'}", color=c["fg"].name())
        st = self.stats[self.current]
        self.single_info.setText(f"peak {st['peak_ppm']:.2f} ppm · SNR {st['snr']:.0f} · "
                                 f"Δf {st['df_hz']:+.1f} Hz · corr {st['corr']:.3f}")
        self.include_box.setChecked(on)

    def refresh_average(self):
        c = self.colors()
        sel, lo, hi = self.window()
        ppm = self.spectra[0].ppm[sel]
        sw = self.spectra[0].sw
        pw = self.avg_plot
        pw.clear()
        align = self.align_box.isChecked()
        fid_of = mrs_io.aligned_fid if align else (lambda sp: sp.fid)
        if self.show_excl_box.isChecked():
            for i, sp in enumerate(self.spectra):
                if not self.included[i]:
                    pw.plot(ppm, self.display(fid_of(sp), sw)[sel], pen=pg.mkPen(c["muted"], width=0.8, style=Qt.DashLine))
        chosen = self.selected()
        if self.show_indiv_box.isChecked():
            faint = QColor(c["accent"])
            faint.setAlpha(90)
            for sp in chosen:
                pw.plot(ppm, self.display(fid_of(sp), sw)[sel], pen=pg.mkPen(faint, width=0.8))
        if align:
            pw.addItem(pg.InfiniteLine(mrs_io.WATER_PPM, angle=90,
                                       pen=pg.mkPen(c["muted"], width=1, style=Qt.DotLine)))
        if chosen:
            mean = np.mean([fid_of(sp) for sp in chosen], axis=0)
            pw.plot(ppm, self.display(mean, sw)[sel], name=f"mean of {len(chosen)}",
                    pen=pg.mkPen(c["mean"], width=2.2))
            single = np.median([self.stats[i]["snr"] for i, on in enumerate(self.included) if on])
            mean_snr = mrs_io.snr(np.fft.fftshift(np.fft.fft(mean)))
            self.avg_info.setText(f"{len(chosen)} averages · SNR {mean_snr:.0f} "
                                  f"(median single {single:.0f}, gain ×{mean_snr / single:.2f}, "
                                  f"ideal ×{np.sqrt(len(chosen)):.2f})")
        else:
            self.avg_info.setText("No averages selected")
        pw.setXRange(lo, hi, padding=0)
        pw.enableAutoRange(axis="y")
        pw.setTitle("Mean of selected averages (complex FID average"
                    + (f", peaks aligned to {mrs_io.WATER_PPM:.2f} ppm)" if align else ")"), color=c["fg"].name())

    # ---------- save ----------
    def save_average(self):
        chosen = self.selected()
        if not chosen:
            return
        src = chosen[0].path.parent
        excl = [sp.instance for sp, on in zip(self.spectra, self.included) if not on]
        summary = (f"Averaging {len(chosen)} of {len(self.spectra)} averages"
                   + (f" (excluding {mrs_io.ranges(excl)})" if excl else "") + ".")
        desc = f"{chosen[0].ds.get('SeriesDescription', '')} MEAN{len(chosen)}"[:64]
        dlg = SaveDialog(self, src.with_name(f"{src.name}_avg{len(chosen)}"), desc, summary)
        if dlg.exec() != QDialog.Accepted:
            return
        out_dir = Path(dlg.dir_edit.text())
        if (out_dir / "IMG00001.dcm").exists():
            if QMessageBox.question(self, APP_NAME, f"{out_dir / 'IMG00001.dcm'} already exists. Overwrite?") \
                    != QMessageBox.Yes:
                return
        try:
            out = mrs_io.save_average(chosen, out_dir, dlg.desc_edit.text(), total=len(self.spectra))
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, f"Could not save:\n{e}")
            return
        box = QMessageBox(QMessageBox.Information, APP_NAME, f"Saved mean of {len(chosen)} averages to\n{out}",
                          QMessageBox.Ok, self)
        open_btn = box.addButton("Open folder", QMessageBox.ActionRole)
        box.exec()
        if box.clickedButton() is open_btn:
            os.startfile(out_dir)


def main():
    app = QApplication(sys.argv)
    app.setStyle("windows11")
    app.setApplicationName(APP_NAME)
    icon = Path(__file__).with_name("specview.ico")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    pg.setConfigOptions(antialias=True)
    win = MainWindow()
    win.set_theme(win.theme_combo.currentText())
    win.show()
    if len(sys.argv) > 1:
        win.open_folder(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
