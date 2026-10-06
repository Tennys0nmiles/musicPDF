"""MuseScore: transpose a MusicXML score and engrave it (PDF, .mscz) or play it (MP3)."""
import base64, json, os, subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
MUSESCORE = HERE / "vendor/musescore/AppRun"
STYLE = HERE / "style.mss"
ENV = dict(os.environ, QT_QPA_PLATFORM="offscreen")


def target_key(fifths: int, semitones: int) -> int:
    """Key signature (in fifths) after transposing, spelled with the fewest accidentals."""
    pc = (fifths * 7 + semitones) % 12
    options = [f for f in range(-7, 8) if (f * 7) % 12 == pc]
    return min(options, key=lambda f: (abs(f), f > 0))


def first_key(xml: Path) -> int:
    fifths = ET.parse(xml).getroot().find(".//key/fifths")
    return int(fifths.text) if fifths is not None else 0


def _run(*args: str) -> subprocess.CompletedProcess:
    """Run MuseScore; a stuck run (it once stalled for two minutes) becomes an error."""
    try:
        return subprocess.run([str(MUSESCORE), *args], env=ENV, check=True, capture_output=True, text=True,
                              timeout=300)
    except subprocess.TimeoutExpired:
        raise RuntimeError("MuseScore did not finish within 5 minutes") from None


def musescore(xml: Path, semitones: int, out: Path) -> tuple[bytes, bytes]:
    """Transpose `xml` by `semitones` and return (pdf, mscz)."""
    if semitones == 0:
        pdf, mscz = out / "out.pdf", out / "out.mscz"
        for target in (pdf, mscz):
            _run("-S", str(STYLE), "-o", str(target), str(xml))
        return pdf.read_bytes(), mscz.read_bytes()
    if abs(semitones) == 12:
        opts = {"mode": "by_interval", "transposeInterval": 25}  # perfect octave
    else:
        opts = {"mode": "to_key", "targetKey": target_key(first_key(xml), semitones)}
    opts.update(direction="up" if semitones > 0 else "down", transposeKeySignatures=True,
                transposeChordNames=True, useDoubleSharpsFlats=False)
    proc = _run("-S", str(STYLE), str(xml), "--score-transpose", json.dumps(opts))
    data = json.loads(proc.stdout[proc.stdout.index("{"):])
    return base64.b64decode(data["pdf"]), base64.b64decode(data["mscz"])


def export(score: Path, target: Path):
    """Convert with MuseScore; the format follows target's extension (.mp3, .mid, .pdf...).
    Audio uses the bundled basic sounds, so no download or account is needed."""
    args = ["-o", str(target), str(score)]
    if target.suffix.lower() in (".mp3", ".wav", ".ogg", ".flac"):
        args = ["--sound-profile", "MuseScore Basic", *args]
    _run(*args)
