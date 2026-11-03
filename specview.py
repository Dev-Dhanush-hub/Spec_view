"""Quick viewer for Siemens MR Spectroscopy DICOMs (SVS)."""
import sys
from pathlib import Path

import numpy as np
import pydicom
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

WATER_PPM = 4.7


def load_fid(path):
    ds = pydicom.dcmread(path)
    raw = np.frombuffer(ds.SpectroscopyData, dtype="<f4")
    fid = raw[0::2] + 1j * raw[1::2]
    return ds, fid


def to_spectrum(ds, fid):
    sw = float(ds.SpectralWidth)
    f0 = float(ds.TransmitterFrequency)
    n = fid.size
    spec = np.fft.fftshift(np.fft.fft(fid))
    hz = np.fft.fftshift(np.fft.fftfreq(n, d=1 / sw))
    ppm = WATER_PPM - hz / f0
    return ppm, spec


def summarize(path, ds, fid, ppm, spec):
    mag = np.abs(spec)
    noise = np.std(np.real(spec[: n_edge(spec)]))
    ip = int(np.argmax(mag))
    print(f"== {Path(path).name}")
    for tag in ("SeriesDescription", "ResonantNucleus", "TransmitterFrequency",
                "SpectralWidth", "EchoTime", "RepetitionTime",
                "NumberOfAverages", "SpectroscopyAcquisitionDataColumns",
                "DataPointColumns", "DataPointRows", "Rows", "Columns",
                "NumberOfFrames"):
        if tag in ds:
            print(f"  {tag}: {ds.data_element(tag).value}")
    print(f"  complex points: {fid.size}")
    print(f"  |FID| first/max/last: {abs(fid[0]):.4g} / {np.abs(fid).max():.4g} / {abs(fid[-1]):.4g}")
    print(f"  tallest peak: {ppm[ip]:.2f} ppm, peak/noise ~ {mag[ip] / noise:.1f}")


def n_edge(spec):
    return max(16, spec.size // 16)


def plot_series(paths, out_dir):
    """One panel per individual average, plus an overlay and their mean."""
    items = []
    for path in paths:
        ds, fid = load_fid(path)
        ppm, spec = to_spectrum(ds, fid)
        items.append((int(ds.InstanceNumber), ppm, spec))
    items.sort(key=lambda it: it[0])
    ppm = items[0][1]
    specs = np.array([it[2] for it in items])

    ncol = 8
    nrow = int(np.ceil(len(items) / ncol))
    fig, axes = plt.subplots(nrow, ncol, figsize=(2.6 * ncol, 2.2 * nrow), sharex=True, sharey=True)
    for ax, (inst, _, spec) in zip(axes.flat, items):
        ax.plot(ppm, np.abs(spec), lw=0.6)
        ax.set_title(f"avg {inst}", fontsize=9)
        ax.set_xlim(8, -1)
        ax.tick_params(labelsize=7)
    for ax in list(axes.flat)[len(items):]:
        ax.axis("off")
    fig.supxlabel("ppm")
    fig.tight_layout()
    grid_out = out_dir / "averages_grid.png"
    fig.savefig(grid_out, dpi=110)

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(14, 5))
    for inst, _, spec in items:
        a1.plot(ppm, np.abs(spec), lw=0.5, color=plt.cm.viridis(inst / len(items)))
    a1.plot(ppm, np.abs(specs.mean(axis=0)), lw=1.4, color="k", label="mean of averages")
    a1.set(title="All averages (colour = order) + mean", xlabel="ppm", xlim=(8, -1))
    a1.legend()
    # zoom below water so metabolite/lipid region is visible
    sel = (ppm > -0.5) & (ppm < 4.2)
    for inst, _, spec in items:
        a2.plot(ppm[sel], np.abs(spec[sel]), lw=0.5, color=plt.cm.viridis(inst / len(items)))
    a2.plot(ppm[sel], np.abs(specs.mean(axis=0)[sel]), lw=1.4, color="k")
    a2.set(title="Zoom 4.2 to -0.5 ppm", xlabel="ppm", xlim=(4.2, -0.5))
    fig.tight_layout()
    overlay_out = out_dir / "averages_overlay.png"
    fig.savefig(overlay_out, dpi=110)

    peak = np.abs(specs).max(axis=1)
    print(f"{len(items)} averages; water peak height min/mean/max: "
          f"{peak.min():.3g} / {peak.mean():.3g} / {peak.max():.3g}")
    print(f"saved {grid_out}\nsaved {overlay_out}")


def main(paths):
    if len(paths) > 2:
        plot_series(paths, Path(__file__).parent)
        return
    fig, axes = plt.subplots(len(paths), 2, figsize=(13, 4 * len(paths)), squeeze=False)
    for row, path in zip(axes, paths):
        ds, fid = load_fid(path)
        ppm, spec = to_spectrum(ds, fid)
        summarize(path, ds, fid, ppm, spec)
        t = np.arange(fid.size) / float(ds.SpectralWidth) * 1e3
        row[0].plot(t, np.real(fid), lw=0.7, label="real")
        row[0].plot(t, np.abs(fid), lw=0.7, label="|FID|")
        row[0].set(title=f"{Path(path).name}  FID ({ds.SeriesDescription})", xlabel="ms")
        row[0].legend()
        row[1].plot(ppm, np.abs(spec), lw=0.8, label="magnitude")
        row[1].plot(ppm, np.real(spec), lw=0.6, alpha=0.7, label="real (unphased)")
        row[1].set(title="Spectrum", xlabel="ppm", xlim=(8, -1))
        row[1].legend()
    fig.tight_layout()
    out = Path(__file__).with_name("spectra.png")
    fig.savefig(out, dpi=110)
    print(f"saved {out}")


if __name__ == "__main__":
    args = sys.argv[1:] or sorted(str(p) for p in Path(__file__).parent.glob("P1/*.dcm"))
    main(args)
