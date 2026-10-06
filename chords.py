"""Read chord symbols (e.g. F#m7/E, Bb7b9, N.C.) from the band above each system.

Audiveris' own OCR drops most chord names with sharps/flats/superscripts, so we
segment the text above the top staff ourselves, enlarge superscripts to full
size, OCR each word with Tesseract, and keep only words that parse as chords.
"""
import os, re, subprocess, tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

from omr import Book, System

HERE = Path(__file__).resolve().parent
TESS = HERE / "vendor/tesseract"
WHITELIST = "ABCDEFGNmajdisugb#/0123456789()+."

QUALITY = r"(?P<q>maj|m|min|dim|o|aug|\+|sus)?"
# One plain extension (7, 9, 6/9, 13...), then only altered (b9, #5) or added tones.
EXT = r"(?P<e>(?:6/?9|13|11|9|7|6|5|4|2)?(?:[#b](?:13|11|9|5)|add(?:13|11|9|4|2)|sus[24]?|aug|dim)*)"
CHORD_RE = re.compile(rf"^(?P<r>[A-G])(?P<a>[#b]?){QUALITY}{EXT}(?:/(?P<br>[A-G])(?P<ba>[#b]?))?$")


@dataclass
class Chord:
    measure: int
    offset: Fraction  # from measure start, in whole notes
    text: str         # normalized, e.g. "F#m7/E" or "N.C."


@dataclass
class _Word:
    sheet: int
    system: System
    box: tuple[int, int, int, int]  # x0, y0, x1, y1 in page pixels
    baseline: int
    img: np.ndarray                 # normalized (superscripts enlarged), ink=True
    raw: np.ndarray                 # original word pixels, ink=True
    reads: list = None              # chord parses from several OCR variants (None = unreadable)
    plain: str = ""                 # unrestricted OCR, to spot ordinary words ("Free time")
    label: str | None = None        # consensus of `reads`


# ---------------------------------------------------------------- segmentation

def _band(system: System, prev: System | None, height: int) -> tuple[int, int]:
    il = system.interline
    top = system.staves[0][0]
    y0 = top - 10 * il
    if prev is not None and prev.sheet == system.sheet:
        y0 = max(y0, prev.staves[-1][1] + 2 * il)
    return max(int(y0), 0), min(int(top - 0.6 * il), height)


def _words(page: np.ndarray, system: System, prev: System | None) -> list[_Word]:
    il = system.interline
    y0, y1 = _band(system, prev, page.shape[0])
    if y1 - y0 < 2 * il:
        return []
    ink = page[y0:y1] < 128
    lab, _ = ndimage.label(ink, structure=np.ones((3, 3)))
    comps = []
    for i, sl in enumerate(ndimage.find_objects(lab), 1):
        h, w = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
        if sl[0].start == 0 or sl[0].stop >= ink.shape[0] - 1:
            continue  # cut by the band edge: stems, slurs, notes of the staff below
        if not (0.25 * il <= h <= 2.6 * il) or w > 5 * il or (w > 3 * h and h < 0.5 * il):
            if not (h < 0.4 * il and w < 0.4 * il and h > 0.12 * il):  # keep dots ("N.C.")
                continue
        comps.append([sl[1].start, sl[0].start, sl[1].stop, sl[0].stop, i])
    comps.sort()
    # Group left-to-right into words: vertically overlapping, small horizontal gap.
    groups: list[list[list[int]]] = []
    for c in comps:
        for g in groups:
            gx1 = max(k[2] for k in g)
            gy0, gy1 = min(k[1] for k in g), max(k[3] for k in g)
            if c[0] - gx1 <= 0.75 * il and c[1] < gy1 and c[3] > gy0 - 0.3 * il:
                g.append(c)
                break
        else:
            groups.append([c])
    words = []
    for g in groups:
        x0, gy0 = min(k[0] for k in g), min(k[1] for k in g)
        x1, gy1 = max(k[2] for k in g), max(k[3] for k in g)
        if gy1 - gy0 < 0.8 * il:
            continue  # only small marks (dots, dashes)
        mask = np.isin(lab[gy0:gy1, x0:x1], [k[4] for k in g])
        img, base = _normalize(mask, [(k[0] - x0, k[1] - gy0, k[2] - x0, k[3] - gy0) for k in g])
        words.append(_Word(system.sheet, system, (x0, y0 + gy0, x1, y0 + gy1), y0 + gy0 + base, img, mask))
    return words


def _normalize(mask: np.ndarray, boxes: list[tuple[int, int, int, int]]) -> tuple[np.ndarray, int]:
    """Make a chord word OCR-friendly: superscripts (7, 9, b5) enlarged onto the baseline."""
    hmax = max(b[3] - b[1] for b in boxes)
    # Baseline: the bottom edge shared by the most pixel columns. Per-column (not per
    # glyph) because tightly set glyphs fuse, e.g. "m/D" with the slash's descender.
    inked = np.flatnonzero(mask.any(0))
    col_bot = mask.shape[0] - np.argmax(mask[::-1, inked], axis=0)
    col_top = np.argmax(mask[:, inked], axis=0)
    tol = max(2, int(0.06 * hmax))
    base = int(max(set(col_bot), key=lambda y: (np.sum(np.abs(col_bot - y) <= tol), y)))
    at_base = np.abs(col_bot - base) <= tol
    cap = int(np.percentile(base - col_top[at_base], 90)) if at_base.any() else hmax  # capital height
    dot = lambda b: b[3] - b[1] < 0.25 * cap and b[2] - b[0] < 0.25 * cap
    raised = lambda b: b[3] < base - 0.2 * cap and b[3] - b[1] < 0.85 * cap
    # Merge x-overlapping glyphs into columns (keeps i/j dots with their stem).
    cols: list[list] = []
    for b in sorted(boxes):
        if cols and cols[-1][1] - b[0] > 0.4 * (b[2] - b[0]):  # mostly on top of the column
            cols[-1][1] = max(cols[-1][1], b[2])
            cols[-1][2].append(b)
        else:
            cols.append([b[0], b[2], [b]])
    kinds = []
    for _, _, bs in cols:
        big = [b for b in bs if not dot(b)]
        kinds.append(bool(big) and all(raised(b) for b in big) if big else (kinds[-1] if kinds else False))
    out_h = max(mask.shape[0], base + 1) + 4
    gap = np.zeros((out_h, max(int(0.12 * cap), 3)), bool)
    out = []
    for (x0, x1, _), sup in zip(cols, kinds):
        seg = mask[:, x0:x1]
        canvas = np.zeros((out_h, seg.shape[1]), bool)
        if sup:
            rows = np.nonzero(seg.any(1))[0]
            seg = seg[rows[0]:rows[-1] + 1]
            f = min(max(0.95 * cap / seg.shape[0], 1.0), 2.0)
            size = (max(int(seg.shape[1] * f), 1), max(int(seg.shape[0] * f), 1))
            seg = np.array(Image.fromarray(seg.astype(np.uint8) * 255).resize(size)) > 127
            canvas = np.zeros((out_h, seg.shape[1]), bool)
            top = max(base - seg.shape[0], 0)
            canvas[top:top + seg.shape[0]] = seg[:out_h - top]
        else:
            canvas[:seg.shape[0]] = seg
        out += [canvas, gap]
    return np.hstack(out), base


# ------------------------------------------------------------------------- OCR

def _tesseract(png: Path, model: str = "tessdata", *config: str) -> str:
    env = dict(os.environ, LD_LIBRARY_PATH=str(TESS), TESSDATA_PREFIX=str(TESS / model), OMP_THREAD_LIMIT="1")
    args = [str(TESS / "tesseract"), str(png), "-", "--psm", "7"]
    for c in config:
        args += ["-c", c]
    return subprocess.run(args, capture_output=True, text=True, env=env).stdout.strip()


# Each variant drops different characters (sharps, flats, superscripts); combined they agree.
VARIANTS = [(model, scale, norm) for model in ("tessdata", "tessdata_best") for scale in (2, 3) for norm in (True, False)]


def _ocr(word: _Word, tmp: Path, n: int):
    word.reads = []
    for v, (model, scale, norm) in enumerate(VARIANTS):
        img = np.pad(~(word.img if norm else word.raw), 12, constant_values=True)
        f = tmp / f"w{n}_{v}.png"
        Image.fromarray(img.astype(np.uint8) * 255).resize((img.shape[1] * scale, img.shape[0] * scale)).save(f)
        text = _tesseract(f, model, f"tessedit_char_whitelist={WHITELIST}", "load_system_dawg=0", "load_freq_dawg=0")
        word.reads.append(parse(text))
        if v == 0:
            word.plain = _tesseract(f)
    # "maj" whose superscript 7 the restricted pass dropped but the plain pass kept as ' or ’.
    if re.search(r"maj[’'?7]", word.plain):
        word.reads = [r and re.sub(r"maj(?!\d)", "maj7", r) for r in word.reads]
    word.label = None if _is_prose(word.plain) else _consensus(word.reads)


def _align(short: str, long: str) -> list[int] | None:
    """Positions of `long` matched by `short` as a subsequence, or None."""
    pos, j = [], 0
    for ch in short:
        while j < len(long) and long[j] != ch:
            j += 1
        if j == len(long):
            return None
        pos.append(j)
        j += 1
    return pos


def _supersequences(a: str, b: str) -> str | None:
    """Shortest chord containing both a and b when they differ only by #, b or digits."""
    i = j = 0
    out = []
    while i < len(a) or j < len(b):
        if i < len(a) and j < len(b) and a[i] == b[j]:
            out.append(a[i]); i += 1; j += 1
        elif i < len(a) and a[i] in "#b0123456789" and (j >= len(b) or a[i] != b[j]):
            out.append(a[i]); i += 1
        elif j < len(b) and b[j] in "#b0123456789":
            out.append(b[j]); j += 1
        else:
            return None
    m = "".join(out)
    return m if parse(m) == m else None


def _consensus(reads: list, min_support: int = 2, min_consistent: int = 3) -> str | None:
    """Reading consistent with the most OCR variants, every character seen at least twice.

    OCR errors here are mostly dropped characters, so a read supports any
    candidate it is a subsequence of; invented characters rarely repeat.
    """
    rs = [r for r in reads if r]
    if not rs:
        return None
    cands = set(rs) | {_supersequences(a, b) for a in set(rs) for b in set(rs) if a < b}
    best = None
    for c in cands - {None}:
        support, consistent = [0] * len(c), 0
        for r in rs:
            pos = _align(r, c)
            if pos is not None:
                consistent += 1
                for p in pos:
                    support[p] += 1
        if min(support) < min(min_support, len(rs)):
            continue
        key = (consistent, len(c))
        if best is None or key > best[0]:
            best = (key, c)
    # A lone reading is noise (a tuplet "4" read as "A" by one variant).
    return best[1] if best and best[0][0] >= min_consistent else None


def _is_prose(plain: str) -> bool:
    """Free OCR reads ordinary words (Free time, a tempo, Brass) as 4+ lowercase letters."""
    for run in re.findall(r"[a-z]{4,}", plain):
        if not re.fullmatch(r"(m|maj|min|dim|aug|sus|add)+", run):
            return True
    return False


def parse(text: str) -> str | None:
    """Normalize OCR text to a chord symbol, or None if it is not one."""
    t = text.replace(" ", "")
    if re.fullmatch(r"N\.?C\.?", t):
        return "N.C."
    if not t or t[0] not in "ABCDEFG":
        return None
    # A sharp misread as a lowercase letter right after a root: F[i|g|d]m -> F#m.
    # Enlarged superscript flat after a note letter reads as ) j }: Fm/E) -> Fm/Eb.
    t = re.sub(r"(?<=[A-G])[)j}](?=m|\d|/|$)", "b", t)
    # A sharp misread as a letter that cannot follow a root: Fim, FAm, GEm7b5, F4m -> F#...
    t = re.sub(r"(^|/)([A-G])(?:[A-Zgiu4]|(?=[a-z])[^abdmosu])", r"\1\2#", t)
    t = re.sub(r"(?<=[#b])s", "5", t)  # superscript 5 read as s: m7bs -> m7b5
    t = re.sub(r"ma(?=\d)", "maj", t).replace("M7", "maj7")
    if not CHORD_RE.match(t) or t.count("(") != t.count(")"):
        return None
    return t


def _to_time(word: _Word) -> tuple[int, Fraction]:
    il = word.system.interline
    x = word.box[0] + 0.35 * (word.baseline - word.box[1]) if word.box[3] - word.box[1] > il else word.box[0]
    stacks = word.system.stacks
    st = next((s for s in stacks if s.left <= x < s.right), stacks[0] if x < stacks[0].left else stacks[-1])
    if not st.slots:
        return st.measure, Fraction(0)
    _, off = min(st.slots, key=lambda s: abs(s[0] - x))
    return st.measure, off


def _read_words(book: Book, progress=None) -> list[_Word]:
    words, prev = [], None
    for system in book.systems:
        words += _words(book.pages[system.sheet - 1], system, prev)
        prev = system
    # 8 workers: each Tesseract peaks around 80 MB, so this stays well under 1 GB.
    with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(min(os.cpu_count() or 4, 8)) as ex:
        futures = [ex.submit(_ocr, w, Path(tmp), i) for i, w in enumerate(words)]
        for k, f in enumerate(as_completed(futures), 1):
            f.result()
            if progress:
                progress(k / len(futures), f"Reading chord names: {k} of {len(futures)}")
    return words


def _signature(word: _Word, page: np.ndarray) -> np.ndarray:
    x0, y0, x1, y1 = word.box
    return page[y0:y1, x0:x1] < 128


def _same_glyphs(a: np.ndarray, b: np.ndarray) -> bool:
    """True if two word images show the same characters (scan noise tolerated)."""
    (ha, wa), (hb, wb) = a.shape, b.shape
    if abs(ha - hb) > 0.12 * max(ha, hb) or abs(wa - wb) > 0.06 * max(wa, wb):
        return False
    h, w = 48, max(int(48 * (wa + wb) / (ha + hb)), 8)
    ra = np.array(Image.fromarray(a.astype(np.uint8) * 255).resize((w, h))) > 127
    rb = np.array(Image.fromarray(b.astype(np.uint8) * 255).resize((w, h))) > 127
    diff = ndimage.binary_opening(ra ^ rb, np.ones((3, 3)))  # drop 1-px edge jitter
    return diff.sum() < 0.08 * min(ra.sum(), rb.sum())


def _vote(words: list[_Word], book: Book) -> dict[int, str]:
    """Pool the readings of words that look identical (the same chord printed again)."""
    sigs = [_signature(w, book.pages[w.sheet - 1]) for w in words]
    parent = list(range(len(words)))
    find = lambda i: i if parent[i] == i else find(parent[i])
    for i in range(len(words)):
        for j in range(i):
            if find(i) != find(j) and _same_glyphs(sigs[i], sigs[j]):
                parent[find(i)] = find(j)
    pooled: dict[int, list] = {}
    for i, w in enumerate(words):
        if w.label:
            pooled.setdefault(find(i), []).extend(w.reads)
    labels = {}
    for i, w in enumerate(words):
        if _is_prose(w.plain) or not any(w.reads):
            continue
        group = pooled.get(find(i))
        best = _consensus(group, 3) if group else None
        # Pooling may only restore characters this word's OCR dropped (Fm/E -> F#m/E),
        # never swap letters: E/F or 7/9 differ by a few pixels and can cluster together.
        if best and (w.label is None or _align(w.label, best) is not None):
            labels[i] = best
        elif w.label:
            labels[i] = w.label
    return labels


def detect(book: Book, progress=None) -> list[Chord]:
    words = _read_words(book, progress)
    labels = _vote(words, book)
    chords = []
    for system in book.systems:
        ws = [(w, labels[i]) for i, w in enumerate(words) if w.system is system and i in labels]
        if not ws:
            continue
        # Chord names share one text line; drop stray matches on other lines (titles, tempo).
        il = system.interline
        lines = Counter(round(w.baseline / (1.2 * il)) for w, p in ws if p != "N.C.")
        best = max(lines, key=lambda k: (lines[k], k)) if lines else None
        for w, p in ws:
            if p == "N.C." or best is None or abs(round(w.baseline / (1.2 * il)) - best) <= 1:
                chords.append(Chord(*_to_time(w), p))
    return chords
