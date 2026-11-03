"""Average individual SVS averages (one FID per DICOM) into a single DICOM.

The FIDs are averaged coherently (complex mean, no phase/frequency alignment),
which is what the scanner does when it combines averages online.
"""
import sys
from pathlib import Path

from mrs_io import load_spectrum, save_average


if __name__ == "__main__":
    here = Path(__file__).parent
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else here / "P1"
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_name(src.name + "_avg")
    spectra = sorted((load_spectrum(p) for p in src.glob("*.dcm")), key=lambda sp: sp.instance)
    out = save_average(spectra, dst)
    print(f"averaged {len(spectra)} FIDs (instances {spectra[0].instance}-{spectra[-1].instance}) -> {out}")
