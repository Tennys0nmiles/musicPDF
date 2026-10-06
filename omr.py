"""Run Audiveris on cleaned page images and read back its geometry (.omr book)."""
import re, subprocess, unicodedata, zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import numpy as np
from PIL import Image

from clean import erase_fretboards

HERE = Path(__file__).resolve().parent
AUDIVERIS = HERE / "vendor/audiveris/opt/audiveris/bin/Audiveris"
DPI = 300
STEPS = 20  # Audiveris logs 20 steps per page (LOAD ... PAGE)
STEP_LINE = re.compile(r"\[\w+#(\d+)\]\s+StepMonitoring\b.*\|\s*([A-Z_]+)\s*$")


@dataclass
class Stack:
    """One measure column of a system, in page pixels."""
    measure: int                      # 0-based index into the exported MusicXML measures
    left: int
    right: int
    slots: list[tuple[int, Fraction]]  # (absolute x, time offset in whole notes)


@dataclass
class System:
    sheet: int                        # 1-based page number
    staves: list[tuple[float, float]]  # (top line y, bottom line y) per staff, top to bottom
    stacks: list[Stack]

    @property
    def interline(self) -> float:
        top, bottom = self.staves[0]
        return (bottom - top) / 4


@dataclass
class LyricItem:
    sheet: int
    system: int    # index into Book.systems
    x: int
    y: int         # baseline
    kind: str      # Syllable | Hyphen (Audiveris files "_" extenders as Hyphen too)
    value: str


@dataclass
class Book:
    mxl: Path
    pages: list[np.ndarray]           # cleaned grayscale page images at DPI
    systems: list[System] = field(default_factory=list)
    chord_names: list[tuple[int, str, tuple[int, int, int, int]]] = field(default_factory=list)  # (sheet, text, x,y,w,h)
    lyrics: list[LyricItem] = field(default_factory=list)


def rasterize(pdf: Path, workdir: Path) -> list[np.ndarray]:
    r = subprocess.run(["pdftoppm", "-r", str(DPI), "-gray", "-png", str(pdf), str(workdir / "page")],
                       capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("couldn't open that file as a PDF")
    return [np.array(Image.open(p).convert("L")) for p in sorted(workdir.glob("page-*.png"))]


def run(pdf: Path, workdir: Path, progress=None) -> Book:
    """`progress(fraction, message)` is called as Audiveris works through the pages."""
    pages = [erase_fretboards(g, DPI)[0] for g in rasterize(pdf, workdir)]
    tif = workdir / "score.tif"
    imgs = [Image.fromarray(p).convert("1") for p in pages]
    imgs[0].save(tif, save_all=True, append_images=imgs[1:], compression="group4", dpi=(DPI, DPI))
    proc = subprocess.Popen([str(AUDIVERIS), "-batch", "-export", "-output", str(workdir), "--", str(tif)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    done: dict[int, int] = {}  # page -> steps finished
    for line in proc.stdout:
        m = STEP_LINE.search(line)
        if m and progress:
            page = int(m.group(1))
            done[page] = done.get(page, 0) + 1
            progress(min(sum(done.values()) / (STEPS * len(pages)), 1.0),
                     f"Reading the music: page {page} of {len(pages)}")
    if proc.wait():
        raise RuntimeError(f"Audiveris failed (exit code {proc.returncode})")
    if not (workdir / "score.mxl").exists():
        raise RuntimeError("Audiveris found no music in the PDF")
    return load(workdir / "score.mxl", workdir / "score.omr", pages)


def load(mxl: Path, omr: Path, pages: list[np.ndarray]) -> Book:
    book = Book(mxl=mxl, pages=pages)
    _read_book(omr, book)
    return book


def _text(s: str) -> str:
    return unicodedata.normalize("NFKC", s)


def _read_book(omr: Path, book: Book):
    z = zipfile.ZipFile(omr)
    offset = 0
    for sheet in range(1, len(book.pages) + 1):
        name = f"sheet#{sheet}/sheet#{sheet}.xml"
        if name not in z.namelist():
            continue  # Audiveris skipped this page (no staves)
        root = ET.fromstring(z.read(name))
        page = root.find(".//page")
        for sysx in root.iter("system"):
            stacks = []
            for st in sysx.findall("stack"):
                if st.get("special"):  # cautionary key/time "measure" at line end: merged on export
                    continue
                left = int(st.get("left"))
                slots = [(left + int(sl.get("x-offset")), Fraction(sl.get("time-offset")))
                         for sl in st.findall("slot")]
                stacks.append(Stack(offset + int(st.get("id")) - 1, left, int(st.get("right")), slots))
            staves = []
            for staff in sysx.iter("staff"):
                lines = staff.find("lines")
                if lines is None:
                    continue
                ls = lines.findall("line")
                top = min(float(p.get("y")) for p in ls[0].findall("point"))
                bottom = max(float(p.get("y")) for p in ls[-1].findall("point"))
                staves.append((top, bottom))
            if staves and stacks:
                book.systems.append(System(sheet, staves, stacks))
            for cn in sysx.iter("chord-name"):
                b = cn.find("bounds")
                if b is None:
                    continue
                box = tuple(int(b.get(k)) for k in ("x", "y", "w", "h"))
                book.chord_names.append((sheet, _text(cn.get("value")), box))
            for li in sysx.iter("lyric-item"):
                loc = li.find("location")
                book.lyrics.append(LyricItem(sheet, len(book.systems) - 1, int(loc.get("x")), int(loc.get("y")),
                                             li.get("kind", ""), _text(li.get("value", ""))))
        offset += int(page.get("measure-count", 0)) if page is not None else 0
