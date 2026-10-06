"""MuseScore: transpose a MusicXML score and engrave it (PDF, .mscz) or play it (MP3)."""
import base64, json, os, re, subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
MUSESCORE = HERE / "vendor/musescore/AppRun"
STYLE = HERE / "style.mss"
ENV = dict(os.environ, QT_QPA_PLATFORM="offscreen")


# Key signatures by number of sharps (+) or flats (-), with their major and minor keys.
MAJOR = {-7: "C♭", -6: "G♭", -5: "D♭", -4: "A♭", -3: "E♭", -2: "B♭", -1: "F", 0: "C",
         1: "G", 2: "D", 3: "A", 4: "E", 5: "B", 6: "F♯", 7: "C♯"}
KEYS = list(range(-7, 8))  # every key signature, 7 flats .. 7 sharps
MINOR = {-7: "A♭", -6: "E♭", -5: "B♭", -4: "F", -3: "C", -2: "G", -1: "D", 0: "A",
         1: "E", 2: "B", 3: "F♯", 4: "C♯", 5: "G♯", 6: "D♯", 7: "A♯"}


def key_name(fifths: int) -> str:
    return f"{MAJOR[fifths]} major / {MINOR[fifths]} minor"


def parse_key(text: str) -> int:
    """Key signature (fifths) for a key name: "Am", "A minor", "Bb", "F# major", "E♭m",
    or a pair like "C major / A minor"."""
    t = text.split("/")[0].strip().replace("♯", "#").replace("♭", "b")
    m = re.fullmatch(r"([A-Ga-g])([#b]?)\s*(m|min|minor|maj|major|M)?", t)
    if not m:
        raise ValueError(f"not a key: {text!r} (try e.g. Am, Bb, F# major)")
    tonic = m.group(1).upper() + m.group(2)
    minor = (m.group(3) or "") in ("m", "min", "minor")
    table = MINOR if minor else MAJOR
    for fifths, name in table.items():
        if name.replace("♯", "#").replace("♭", "b") == tonic:
            return fifths
    # Spelled with more than 7 accidentals (D# major): use the enharmonic key.
    pc = ("C D EF G A B".index(tonic[0]) + {"#": 1, "b": -1}.get(tonic[1:], 0)) % 12
    if minor:
        pc = (pc + 3) % 12  # relative major
    return target_key(0, pc)


def semitones_between(src: int, dst: int, direction: str = "closest") -> int:
    """Semitones from key signature src to dst: up, down, or whichever is nearer."""
    up = (7 * (dst - src)) % 12
    if up == 0:
        return 0
    if direction == "up":
        return up
    if direction == "down":
        return up - 12
    return up if up <= 6 else up - 12


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


def musescore(xml: Path, semitones: int, out: Path, key: int | None = None) -> tuple[bytes, bytes]:
    """Transpose `xml` by `semitones` (into key signature `key` if given, which fixes the
    spelling, e.g. G-flat vs F-sharp major) and return (pdf, mscz)."""
    if key is not None and key != first_key(xml):
        opts = {"mode": "to_key", "targetKey": key,
                "direction": "up" if semitones > 0 else "down" if semitones < 0 else "closest"}
        opts.update(transposeKeySignatures=True, transposeChordNames=True, useDoubleSharpsFlats=False)
        proc = _run("-S", str(STYLE), str(xml), "--score-transpose", json.dumps(opts))
        data = json.loads(proc.stdout[proc.stdout.index("{"):])
        return base64.b64decode(data["pdf"]), base64.b64decode(data["mscz"])
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
