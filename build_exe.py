"""Build dist/SpecView/SpecView.exe with PyInstaller (run with the project .venv)."""
import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPainterPath, QPen

HERE = Path(__file__).parent
ICON = HERE / "specview.ico"


def make_icon():
    """A Lorentzian peak on a rounded tile."""
    app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841 (needed for QPainter text/AA)
    img = QImage(256, 256, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(0, 95, 184))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(8, 8, 240, 240, 48, 48)
    path = QPainterPath()
    for i in range(201):
        x = 28 + i
        lor = 1 / (1 + ((i - 100) / 9) ** 2) + 0.25 / (1 + ((i - 150) / 6) ** 2)
        pt = QPointF(x, 212 - 165 * lor)
        path.moveTo(pt) if i == 0 else path.lineTo(pt)
    p.setPen(QPen(QColor("white"), 12, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.drawPath(path)
    p.end()
    img.save(str(ICON))


if __name__ == "__main__":
    make_icon()
    env = dict(os.environ)
    # Conda-based Pythons keep the DLLs of _ctypes/_bz2/... (libffi etc.) here; PyInstaller
    # only bundles them if it can find them on PATH.
    conda_bin = Path(sys.base_prefix) / "Library" / "bin"
    if conda_bin.is_dir():
        env["PATH"] = f"{conda_bin}{os.pathsep}{env.get('PATH', '')}"
    # default: one self-contained SpecView.exe; --folder: SpecView/ folder build (starts faster)
    onefile = "--folder" not in sys.argv
    excludes = ["tkinter", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtPdf", "PySide6.QtNetwork",
                "PySide6.QtWebEngineCore", "PySide6.QtMultimedia"]
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
        "--onefile" if onefile else "--onedir",
        "--name", "SpecView", "--icon", str(ICON),
        "--add-data", f"{ICON};.",
        *[arg for m in excludes for arg in ("--exclude-module", m)],
        str(HERE / "specview_app.py"),
    ], cwd=HERE, env=env, check=True)
    exe = HERE / "dist" / ("SpecView.exe" if onefile else "SpecView/SpecView.exe")
    print(f"built {exe}")
