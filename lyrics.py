"""Repair lyrics in Audiveris MusicXML.

Audiveris gets most syllables right but:
- files the "___" line after a held syllable as a hyphen, gluing "high," to the
  next word ("high,-you"); the two differ in height: a hyphen sits at mid-letter
  height, an extender lies on the baseline;
- reads other markings on the lyric line as text (an 8va "- - -" line, "(8)");
- splits a thin first letter off as a number ("it's" -> "1" + "t's");
- misreads some words ("mine" -> "mme", "butterflies" -> "But-tor-flies").
Words that are not in the dictionary are re-read from the page image.
"""
import difflib, os, re, subprocess, tempfile, unicodedata
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image

from omr import Book, LyricItem

TESS = Path(__file__).resolve().parent / "vendor/tesseract"
DICTIONARIES = ["/usr/share/dict/words", "/usr/share/dict/american-english", "/usr/share/dict/british-english"]


# Two-letter words that occur in lyrics; word lists also hold abbreviations ("pm", "ft")
# that an OCR slip easily produces ("pm" for "I'm").
SHORT_WORDS = {"a", "i", "o", "am", "an", "as", "at", "be", "by", "do", "go", "he", "hi", "if", "in", "is",
               "it", "me", "my", "no", "of", "oh", "ok", "on", "or", "ow", "so", "to", "uh", "up", "us",
               "we", "ya", "yo", "la", "da", "na", "ah", "ha", "ho", "oo", "ay"}


@lru_cache(maxsize=1)
def dictionary() -> frozenset[str]:
    """Lower-case English words, or empty if the system has no word list. Letter plurals
    ("t's") and short abbreviations are left out: they are what OCR slips look like."""
    for path in DICTIONARIES:
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="ignore") as f:
                words = {w.strip().lower().replace("’", "'") for w in f}
            return frozenset(w for w in words
                             if (len(w) > 2 or w in SHORT_WORDS) and not re.fullmatch(r"[a-z]'s", w))
    return frozenset()


def _norm(text: str) -> str:
    """Comparable form of a word: lower case, straight apostrophes, letters only."""
    t = unicodedata.normalize("NFKC", text).replace("’", "'").lower()
    return re.sub(r"[^a-z']", "", t).strip("'")


def is_word(text: str) -> bool:
    words = dictionary()
    return not words or _norm(text) in words


# ---------------------------------------------------------------- page geometry

def _lines(book: Book) -> list[list[LyricItem]]:
    """Lyric items grouped into text lines (a verse line under one staff), left to right.
    Baselines within half a staff space of each other are one line."""
    by_system: dict[tuple, list[LyricItem]] = {}
    for li in book.lyrics:
        by_system.setdefault((li.sheet, li.system), []).append(li)
    lines = []
    for key in sorted(by_system):
        il = book.systems[key[1]].interline if 0 <= key[1] < len(book.systems) else 20
        current: list[LyricItem] = []
        for li in sorted(by_system[key], key=lambda li: li.y):
            if current and li.y - current[0].y > 0.5 * il:
                lines.append(current)
                current = []
            current.append(li)
        if current:
            lines.append(current)
    return [sorted(line, key=lambda li: li.x) for line in lines]


def _is_extender(conn: LyricItem, syl: LyricItem) -> bool:
    """A connector on the baseline is an extender; a hyphen floats at mid-letter height."""
    gap = conn.y - (conn.box[1] + conn.box[3])  # baseline minus the connector's bottom edge
    return gap < 0.15 * max(syl.box[3], 1)


def _syllables(book: Book) -> list[dict]:
    """Syllables in reading order with what follows them and any glued-on prefix digit."""
    out = []
    for items in _lines(book):
        pending_prefix = None
        for i, li in enumerate(items):
            if li.kind == "Number" and li.value in ("1", "l", "I") and i + 1 < len(items):
                nxt = items[i + 1]
                if nxt.kind == "Syllable" and nxt.box[0] - (li.box[0] + li.box[2]) < 0.3 * nxt.box[3]:
                    pending_prefix = li  # e.g. the "i" of "it's" read as "1"
                continue
            if li.kind != "Syllable":
                continue
            nxt = items[i + 1] if i + 1 < len(items) else None
            ext = nxt is not None and nxt.kind not in ("Syllable", "Number") and _is_extender(nxt, li)
            out.append({"item": li, "extender": ext, "prefix": pending_prefix})
            pending_prefix = None
    return out


# ---------------------------------------------------------------- OCR

def _reread(book: Book, li: LyricItem, prefix: LyricItem | None = None) -> str:
    """OCR one syllable again from the page with the more accurate model and a dictionary."""
    x, y, w, h = li.box
    if prefix is not None:
        w += x - prefix.box[0]
        x = prefix.box[0]
    return ocr_box(book, li.sheet, (x, y, w, h), psm=8)


def ocr_box(book: Book, sheet: int, box: tuple[int, int, int, int], psm: int = 7) -> str:
    """OCR a box of the page (x, y, w, h) with the accurate model and its dictionary."""
    x, y, w, h = box
    pad = max(h // 4, 3)
    page = book.pages[sheet - 1]
    crop = page[max(y - pad, 0):y + h + pad, max(x - pad, 0):x + w + pad]
    if crop.size == 0:
        return ""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "w.png"
        img = Image.fromarray(crop)
        img.resize((img.width * 2, img.height * 2)).save(f)
        env = dict(os.environ, LD_LIBRARY_PATH=str(TESS), TESSDATA_PREFIX=str(TESS / "tessdata_best"),
                   OMP_THREAD_LIMIT="1")
        out = subprocess.run([str(TESS / "tesseract"), str(f), "-", "--psm", str(psm)],
                             capture_output=True, text=True, env=env).stdout
    return unicodedata.normalize("NFKC", out.strip())


def _ocr_line(book: Book, sheet: int, box: tuple[int, int, int, int]) -> list[tuple[str, int, int, float]]:
    """Words on one lyric line: (text, x0, x1, confidence), in page coordinates."""
    x0, y0, x1, y1 = box
    page = book.pages[sheet - 1]
    crop = page[max(y0, 0):y1, max(x0, 0):x1]
    if crop.size == 0:
        return []
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "line.png"
        img = Image.fromarray(crop)
        img.resize((img.width * 2, img.height * 2)).save(f)
        env = dict(os.environ, LD_LIBRARY_PATH=str(TESS), TESSDATA_PREFIX=str(TESS / "tessdata_best"),
                   OMP_THREAD_LIMIT="1")
        out = subprocess.run([str(TESS / "tesseract"), str(f), "-", "--psm", "7", "-c", "tessedit_create_tsv=1"],
                             capture_output=True, text=True, env=env).stdout
    words = []
    for row in out.splitlines()[1:]:
        c = row.split("\t")
        if len(c) == 12 and c[11].strip():
            left, width = int(c[6]) // 2 + max(x0, 0), int(c[8]) // 2
            words.append((unicodedata.normalize("NFKC", c[11].strip()), left, left + width, float(c[10])))
    return words


def _recover_missing(score: ET.Element, book: Book) -> int:
    """Put back words Audiveris didn't see: OCR each lyric line under the voice staff, keep
    confident dictionary words that overlap no known syllable, and attach each one to the
    voice note right above it (only if that note has no lyric yet). Systems where Audiveris
    found no lyrics at all get a line predicted from the others."""
    if not dictionary():
        return 0
    parts = score.findall("part")
    if not parts:
        return 0
    measures = parts[0].findall("measure")
    known: dict[int, list] = {}  # system index -> [(baseline, text height, syllables)]
    offsets, heights = [], []
    for line in _lines(book):
        syls = [li for li in line if li.kind == "Syllable"]
        if len(syls) < 2:
            continue
        system = book.systems[syls[0].system]
        base = int(np.median([li.y for li in syls]))
        bottom, il = system.staves[0][1], system.interline
        if bottom < base < bottom + 8 * il:  # lyrics under the top (voice) staff
            th = int(np.median([li.box[3] for li in syls]))
            known.setdefault(syls[0].system, []).append((base, th, syls))
            offsets.append((base - bottom) / il)
            heights.append(th / il)
    added = 0
    for si, system in enumerate(book.systems):
        il = system.interline
        lines = known.get(si)
        predicted = lines is None
        if predicted:
            if len(offsets) < 3:
                continue
            lines = [(int(system.staves[0][1] + np.median(offsets) * il), int(np.median(heights) * il), [])]
        left, right = system.stacks[0].left, system.stacks[-1].right
        staff_top, staff_bottom = system.staves[0]
        heads = [h for h in book.heads if h.sheet == system.sheet and h.part == 0 and h.staff == 1
                 and staff_top - 6 * il < h.box[1] < staff_bottom + 2 * il and left <= h.box[0] <= right]
        for base, th, syls in lines:
            for text, x0, x1, conf in _ocr_line(book, system.sheet, (left, base - int(1.4 * th), right, base + th // 2)):
                # A held syllable's "____" line runs into the word and drags confidence down.
                text = re.sub(r"[_\-—–|]+$", "", re.sub(r"^[_\-—–|]+", "", text))
                if conf < (60 if predicted else 50) or not re.search(r"[A-Za-z]{2,}|^[AaI]$", text) \
                        or not is_word(text):
                    continue
                x1 = min(x1, x0 + int(0.6 * th * len(text)))  # the letters, not the line after them
                if any(min(x1, li.box[0] + li.box[2]) - max(x0, li.box[0]) > 0.3 * min(x1 - x0, li.box[2])
                       for li in syls):
                    continue  # already known
                near = [h for h in heads if x0 - il <= h.box[0] + h.box[2] / 2 <= x1 + 0.3 * il]
                if not near:
                    continue
                target = min(near, key=lambda h: abs(h.box[0] + h.box[2] / 2 - (x0 + x1) / 2))
                if predicted and target.grade < 0.35:
                    continue  # e.g. an instrument label ("Brass") under cue notes
                note = _note_for_head(measures, book, target)
                if note is None or note.find("lyric") is not None:
                    continue
                ly = ET.SubElement(note, "lyric", {"number": "1"})
                ET.SubElement(ly, "syllabic").text = "single"
                ET.SubElement(ly, "text").text = text
                added += 1
    return added


def _note_for_head(measures: list[ET.Element], book: Book, head) -> ET.Element | None:
    """The MusicXML note (voice 1, staff 1, part 1) drawn with this notehead: the k-th chord
    left to right in the measure is the k-th sounding note, if the counts agree."""
    if head.measure >= len(measures):
        return None
    m = measures[head.measure]
    notes = [n for n in m.findall("note") if n.find("chord") is None and n.find("rest") is None
             and n.find("grace") is None and n.findtext("staff", "1") == "1" and n.findtext("voice", "1") == "1"]
    same = sorted((h for h in book.heads if h.sheet == head.sheet and h.measure == head.measure
                   and h.part == head.part and h.staff == head.staff), key=lambda h: h.box[0])
    chords, last_x = [], None  # heads within ~a head width of each other share a stem
    for h in same:
        if last_x is None or h.box[0] - last_x > 1.2 * h.box[2]:
            chords.append([])
        chords[-1].append(h)
        last_x = h.box[0]
    if len(chords) != len(notes):
        return None
    k = next(i for i, c in enumerate(chords) if any(h is head for h in c))
    return notes[k]


# ---------------------------------------------------------------- MusicXML

def _lyrics(score: ET.Element) -> list[ET.Element]:
    return [ly for part in score.findall("part") for ly in part.iter("lyric") if ly.find("text") is not None]


def _set_syllabic(ly: ET.Element, value: str):
    syl = ly.find("syllabic")
    if syl is None:
        syl = ET.Element("syllabic")
        ly.insert(0, syl)
    syl.text = value


def _relink(score: ET.Element):
    """Make begin/middle/end chains consistent after syllables were split or removed."""
    for part in score.findall("part"):
        by_verse: dict[str, list[ET.Element]] = {}
        for ly in part.iter("lyric"):
            if ly.find("text") is not None:
                by_verse.setdefault(ly.get("number", "1"), []).append(ly)
        for seq in by_verse.values():
            for i, ly in enumerate(seq):
                syl = ly.findtext("syllabic", "single")
                prev_open = i > 0 and seq[i - 1].findtext("syllabic", "single") in ("begin", "middle")
                next_cont = i + 1 < len(seq) and seq[i + 1].findtext("syllabic", "single") in ("middle", "end")
                if syl in ("middle", "end") and not prev_open:
                    syl = "begin" if syl == "middle" else "single"
                if syl in ("begin", "middle") and not next_cont:
                    syl = "end" if syl == "middle" else "single"
                _set_syllabic(ly, syl)


def _words(score: ET.Element) -> list[list[ET.Element]]:
    words, cur = [], []
    for ly in _lyrics(score):
        cur.append(ly)
        if ly.findtext("syllabic", "single") in ("single", "end"):
            words.append(cur)
            cur = []
    return words + ([cur] if cur else [])


def fix(score: ET.Element, book: Book):
    lyrics = _lyrics(score)
    # Junk: dashed 8va lines, "(8)", stray dots. A lyric has letters.
    for part in score.findall("part"):
        for note in part.iter("note"):
            for ly in note.findall("lyric"):
                if ly.find("text") is not None and not re.search(r"[^\W\d_]", ly.findtext("text")):
                    note.remove(ly)
    lyrics = _lyrics(score)
    for ly in lyrics:  # "for..." -> "for": dot runs are bits of an extender line
        t = ly.find("text")
        t.text = re.sub(r"\s*\.{2,}$|\s+\.$", "", t.text)

    # Align MusicXML lyrics with Audiveris' lyric items by their text.
    syls = _syllables(book)
    a = [unicodedata.normalize("NFKC", ly.findtext("text")) for ly in lyrics]
    b = [re.sub(r"\s*\.{2,}$|\s+\.$", "", s["item"].value) for s in syls]
    match: dict[int, dict] = {}
    for blk in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        for k in range(blk.size):
            match[blk.a + k] = syls[blk.b + k]

    # Extenders vs hyphens, from where the connector sits.
    for i, ly in enumerate(lyrics):
        s = match.get(i)
        if s is None:
            continue
        syl = ly.findtext("syllabic", "single")
        if s["extender"] and syl in ("begin", "middle"):
            _set_syllabic(ly, "single" if syl == "begin" else "end")
            if ly.find("extend") is None:
                ly.insert(list(ly).index(ly.find("text")) + 1, ET.Element("extend"))
            if i + 1 < len(lyrics):
                nxt = lyrics[i + 1].findtext("syllabic", "single")
                if nxt in ("middle", "end"):
                    _set_syllabic(lyrics[i + 1], "begin" if nxt == "middle" else "single")
    _relink(score)
    _recover_missing(score, book)

    # Spelling: words the dictionary doesn't know are read again from the page.
    if not dictionary():
        return
    item_of = {id(lyrics[i]): s for i, s in match.items()}
    for word in _words(score):
        s = item_of.get(id(word[0]))
        if s is not None and s["prefix"] is not None:  # "1" + "t's": the ink is part of the word
            t = word[0].find("text")
            rest = "".join(ly.findtext("text") for ly in word[1:])
            for p in ("i", "I", "l"):
                if is_word(p + t.text + rest):
                    t.text = p + t.text
                    break
        texts = [ly.findtext("text") for ly in word]
        joined = "".join(texts)
        if not _norm(joined):
            continue
        if is_word(joined):
            # "IS-n't" -> "is-n't": shouting caps inside lowercase lyrics are OCR noise.
            for ly in word:
                t = ly.find("text")
                if len(t.text) > 1 and t.text.isupper() and not joined.isupper():
                    t.text = t.text.lower()
            continue
        candidates = []
        for k, ly in enumerate(word):
            s = item_of.get(id(ly))
            if s is None:
                continue
            if s["prefix"] is not None:  # "1" + "t's" -> "it's"
                candidates += [(k, p + texts[k]) for p in ("i", "I", "l")]
                candidates.append((k, _reread(book, s["item"], s["prefix"])))
            candidates.append((k, _reread(book, s["item"])))
        for k, new in candidates:
            # Keep the original's punctuation: the crop may catch a bit of an extender as "."
            core = re.sub(r"[^\w'’]+$", "", new)
            new = core + re.search(r"[^\w'’]*$", texts[k]).group(0)
            trial = texts[:k] + [new] + texts[k + 1:]
            if new and is_word("".join(trial)):
                word[k].find("text").text = new
                break
