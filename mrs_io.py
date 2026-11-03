"""Loading, metrics and averaging for Siemens single-voxel MRS DICOMs."""
import copy
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pydicom
from pydicom.uid import generate_uid

WATER_PPM = 4.7


@dataclass
class Spectrum:
    path: Path
    ds: pydicom.Dataset
    fid: np.ndarray
    instance: int
    sw: float
    f0: float
    ppm: np.ndarray = field(init=False)
    spec: np.ndarray = field(init=False)

    def __post_init__(self):
        n = self.fid.size
        self.spec = np.fft.fftshift(np.fft.fft(self.fid))
        hz = np.fft.fftshift(np.fft.fftfreq(n, d=1 / self.sw))
        self.ppm = self.ref_ppm - hz / self.f0

    @property
    def ref_ppm(self):
        return float(self.ds.get("ChemicalShiftReference", WATER_PPM))


@dataclass
class Series:
    uid: str
    number: int
    description: str
    spectra: list

    @property
    def label(self):
        return f"{self.number}: {self.description} ({len(self.spectra)})"


def load_fid(path):
    ds = pydicom.dcmread(path)
    raw = np.frombuffer(ds.SpectroscopyData, dtype="<f4")
    return ds, raw[0::2] + 1j * raw[1::2]


def load_spectrum(path):
    ds, fid = load_fid(path)
    return Spectrum(Path(path), ds, fid, int(ds.get("InstanceNumber", 0)),
                    float(ds.SpectralWidth), float(ds.TransmitterFrequency))


def find_series(folder):
    """All MRS series under folder (recursive), largest first."""
    groups = {}
    for path in sorted(Path(folder).rglob("*")):
        if not path.is_file() or path.suffix.lower() not in (".dcm", ".ima", ""):
            continue
        try:
            sp = load_spectrum(path)
        except Exception:
            continue  # not a DICOM, or no SpectroscopyData
        uid = str(sp.ds.get("SeriesInstanceUID", path.parent))
        if uid not in groups:
            groups[uid] = Series(uid, int(sp.ds.get("SeriesNumber", 0)),
                                 str(sp.ds.get("SeriesDescription", path.parent.name)), [])
        groups[uid].spectra.append(sp)
    for s in groups.values():
        s.spectra.sort(key=lambda sp: sp.instance)
    return sorted(groups.values(), key=lambda s: (-len(s.spectra), s.number))


def snr(spec):
    """Tallest magnitude peak over the std of the real part at the spectral edges."""
    edge = max(16, spec.size // 16)
    noise = np.concatenate([spec[:edge].real, spec[-edge:].real])
    return float(np.abs(spec).max() / np.std(noise - noise.mean()))


def metrics(spectra):
    """Per-spectrum peak position/height, SNR, frequency offset and similarity to the median."""
    mags = np.abs(np.array([sp.spec for sp in spectra]))
    ref = np.median(mags, axis=0)
    ref_peak = spectra[0].ppm[int(np.argmax(ref))]
    out = []
    for sp, mag in zip(spectra, mags):
        ip = int(np.argmax(mag))
        out.append({
            "peak_ppm": float(sp.ppm[ip]),
            "height": float(mag[ip]),
            "snr": snr(sp.spec),
            "df_hz": float((ref_peak - sp.ppm[ip]) * sp.f0),
            "corr": float(np.corrcoef(mag, ref)[0, 1]),
        })
    return out


def ranges(nums):
    """[1,2,3,5,7,8] -> '1-3,5,7-8'"""
    nums = sorted(nums)
    parts, start = [], None
    for i, n in enumerate(nums):
        if start is None:
            start = n
        if i == len(nums) - 1 or nums[i + 1] != n + 1:
            parts.append(str(start) if start == n else f"{start}-{n}")
            start = None
    return ",".join(parts)


def mean_fid(spectra):
    return np.mean([sp.fid for sp in spectra], axis=0)


def peak_hz(sp):
    """Frequency (Hz, FFT axis) of the tallest magnitude peak, refined by parabolic interpolation."""
    mag = np.abs(sp.spec)
    i = int(np.argmax(mag))
    delta = 0.0
    if 0 < i < mag.size - 1:
        a, b, c = mag[i - 1], mag[i], mag[i + 1]
        den = a - 2 * b + c
        if den != 0:
            delta = 0.5 * (a - c) / den
    n = sp.fid.size
    hz = np.fft.fftshift(np.fft.fftfreq(n, d=1 / sp.sw))
    return hz[i] + delta * sp.sw / n


def aligned_fid(sp, target_ppm=WATER_PPM):
    """FID frequency-shifted so its tallest peak sits at target_ppm."""
    shift = (sp.ref_ppm - target_ppm) * sp.f0 - peak_hz(sp)
    t = np.arange(sp.fid.size) / sp.sw
    return sp.fid * np.exp(2j * np.pi * shift * t)


def mean_aligned_fid(spectra, target_ppm=WATER_PPM):
    return np.mean([aligned_fid(sp, target_ppm) for sp in spectra], axis=0)


def save_average(spectra, out_dir, series_description=None, total=None):
    """Write the complex mean of the spectra's FIDs as one DICOM in a new series."""
    if not spectra:
        raise ValueError("no spectra selected")
    avg = mean_fid(spectra)
    ds = copy.deepcopy(spectra[0].ds)
    raw = np.empty(avg.size * 2, dtype="<f4")
    raw[0::2] = avg.real
    raw[1::2] = avg.imag
    ds.SpectroscopyData = raw.tobytes()

    n, total = len(spectra), total or len(spectra)
    used = ranges(sp.instance for sp in spectra)
    # new series so viewers/tools don't merge it with the originals
    ds.SOPInstanceUID = generate_uid()
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.SeriesInstanceUID = generate_uid()
    ds.SeriesNumber = int(ds.get("SeriesNumber", 0)) + 1000
    ds.SeriesDescription = (series_description or f"{ds.get('SeriesDescription', '')} MEAN{n}")[:64]
    ds.InstanceNumber = 1
    ds.ImageType = ["DERIVED", "SECONDARY", "SPECTROSCOPY", "NONE"]
    ds.DerivationDescription = f"Complex mean of {n}/{total} individual averages; instances {used}"[:1024]
    ds.ImageComments = f"{ds.get('ImageComments', '')}; mean of {n}/{total} averages ({used})".lstrip("; ")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "IMG00001.dcm"
    ds.save_as(out, enforce_file_format=True)
    return out
