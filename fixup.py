"""Repair Audiveris MusicXML before engraving.

- chord symbols: replace Audiveris' few <harmony> with the ones read by chords.py
- lyrics: see lyrics.py (extenders, junk, misread words)
- notes: an accidental touching its note is sometimes read as a second notehead
- rhythm: quadruplets in compound meter whose "4" Audiveris ignores, false triplets
  (a "3" from nearby text), lost or extra augmentation dots, whole-bar rests
- octave lines (8va): see octaves.py
- dynamics that are really misread text (lyric letters, chord names) are dropped
- flag measures whose voices still don't add up, or whose notes Audiveris was
  unsure of (colored red, reported back)
"""
import difflib, re, unicodedata, zipfile
import xml.etree.ElementTree as ET
from fractions import Fraction

import numpy as np
from scipy import ndimage

import lyrics, octaves
from chords import Chord, CHORD_RE
from omr import Book, Head

FLAG_COLOR = "#E0201B"
SCALE = 4  # durations are multiplied so quadruplet eighths stay integral
LOW_GRADE = 0.35  # Audiveris' own confidence in a notehead below which it is likely wrong


def load_xml(mxl) -> ET.ElementTree:
    z = zipfile.ZipFile(mxl)
    root = ET.fromstring(z.read("META-INF/container.xml"))
    name = root.find(".//{*}rootfile").get("full-path")
    return ET.ElementTree(ET.fromstring(z.read(name)))


def fix(book: Book, chords: list[Chord]) -> tuple[ET.ElementTree, list[int]]:
    """Return the repaired score and the (1-based) numbers of measures still flagged."""
    tree = load_xml(book.mxl)
    score = tree.getroot()
    _scale_durations(score)
    _fix_text(score)
    _fix_credits(score)
    lyrics.fix(score, book)
    _drop_text_dynamics(score)
    _drop_symbols_in_text(score, book)
    _reread_directions(score, book)
    used = _fix_glued_accidentals(score, book)
    octaves.apply(score, book, octaves.find(book),
                  lambda system, k: system.staff_parts[k] if k < len(system.staff_parts) else None)
    for part in score.findall("part"):
        state = {"div": 1, "time": Fraction(3, 4)}
        for m in part.findall("measure"):
            expected = _measure_len(m, state)
            _repair_tuplets(m, expected, state.get("meter", (3, 4)))
            _repair_rhythm(m, expected)
    _set_harmony(score, chords)
    unsure = _unsure_measures(book, used)
    flagged = set()
    for pi, part in enumerate(score.findall("part")):
        state = {"div": 1, "time": Fraction(3, 4)}
        for i, m in enumerate(part.findall("measure")):
            if _flag(m, _measure_len(m, state), (pi, i) in unsure):
                flagged.add(i + 1)
    return tree, sorted(flagged)


# ------------------------------------------------------------------ durations

def _scale_durations(score: ET.Element):
    for el in score.iter():
        if el.tag in ("divisions", "duration") and el.text:
            el.text = str(int(el.text) * SCALE)


def _measure_len(m: ET.Element, state: dict) -> int:
    """Expected measure length in divisions; `state` carries divisions/time across measures."""
    for a in m.findall("attributes"):
        if a.find("divisions") is not None:
            state["div"] = int(a.findtext("divisions"))
        t = a.find("time")
        if t is not None and t.find("beats") is not None:
            state["meter"] = (int(t.findtext("beats")), int(t.findtext("beat-type")))  # 6/8 stays 6/8
            state["time"] = Fraction(*state["meter"])
    return int(state["time"] * 4 * state["div"])


def _voices(m: ET.Element) -> dict[tuple, list[tuple[int, ET.Element]]]:
    """(staff, voice) -> [(onset, note)] for non-chord, non-grace notes, in order."""
    pos, out = 0, {}
    for el in m:
        if el.tag == "backup":
            pos -= int(el.findtext("duration"))
        elif el.tag == "forward":
            pos += int(el.findtext("duration"))
        elif el.tag == "note" and el.find("grace") is None:
            if el.find("chord") is not None:
                continue
            key = (el.findtext("staff", "1"), el.findtext("voice", "1"))
            out.setdefault(key, []).append((pos, el))
            pos += int(el.findtext("duration", "0"))
    return out


def _voice_end(notes: list[tuple[int, ET.Element]]) -> int:
    onset, last = notes[-1]
    return onset + int(last.findtext("duration", "0"))


# ------------------------------------------------------------------ tuplets

def _repair_tuplets(m: ET.Element, expected: int, meter: tuple[int, int]):
    """Turn 4 beamed eighths filling a dotted-quarter beat into a quadruplet when
    the voice is otherwise too long (Audiveris drops the "4" tuplet mark)."""
    if meter[1] != 8 or meter[0] % 3:
        return  # only compound meters (6/8, 9/8, 12/8)
    for notes in _voices(m).values():
        excess = _voice_end(notes) - expected
        i = 0
        while excess > 0 and i + 4 <= len(notes):
            group = [n for _, n in notes[i:i + 4]]
            durs = {int(n.findtext("duration")) for n in group}
            dur = durs.pop() if len(durs) == 1 else 0
            if (dur and dur % 4 == 0 and dur <= excess
                    and len({n.findtext("type") for n in group}) == 1
                    and all(n.find("time-modification") is None for n in group)
                    and group[0].findtext("beam") == "begin" and group[-1].findtext("beam") == "end"):
                _make_tuplet(m, group, 4, 3)  # 4 notes in the time of 3: saves one note value
                _grow_backup_after(m, group[-1], -dur)
                excess -= dur
                i += 4
            else:
                i += 1


def _make_tuplet(m: ET.Element, group: list[ET.Element], actual: int, normal: int):
    members = []
    for n in group:
        members.append(n)
        # chord tones stacked on this note follow it with <chord/>
        idx = list(m).index(n)
        for el in list(m)[idx + 1:]:
            if el.tag == "note" and el.find("chord") is not None:
                members.append(el)
            else:
                break
    for n in members:
        d = n.find("duration")
        d.text = str(int(d.text) * normal // actual)
        tm = ET.Element("time-modification")
        ET.SubElement(tm, "actual-notes").text = str(actual)
        ET.SubElement(tm, "normal-notes").text = str(normal)
        # schema order: ... type, dot*, accidental?, time-modification?, stem? ...
        after = [i for i, c in enumerate(n) if c.tag in ("type", "dot", "accidental")]
        n.insert(after[-1] + 1 if after else len(n), tm)
    for n, kind in ((group[0], "start"), (group[-1], "stop")):
        nots = n.find("notations")
        if nots is None:
            nots = ET.Element("notations")
            pos = [i for i, c in enumerate(n) if c.tag == "lyric"]
            n.insert(pos[0] if pos else len(n), nots)
        ET.SubElement(nots, "tuplet", {"type": kind, "bracket": "yes", "show-number": "actual"} if kind == "start"
                      else {"type": kind})


def _grow_backup_after(m: ET.Element, last: ET.Element, delta: int):
    """A voice changed length by `delta`; the <backup> that rewinds to the measure start must too."""
    children = list(m)
    for el in children[children.index(last) + 1:]:
        if el.tag == "backup":
            d = el.find("duration")
            d.text = str(int(d.text) + delta)
            return
        if el.tag == "forward":
            return


def _chord_of(m: ET.Element, note: ET.Element) -> list[ET.Element]:
    """The note plus the chord tones stacked on it (following notes marked <chord/>)."""
    children = list(m)
    out = [note]
    for el in children[children.index(note) + 1:]:
        if el.tag == "note" and el.find("chord") is not None:
            out.append(el)
        else:
            break
    return out


def _insert_after(n: ET.Element, el: ET.Element, tags: tuple[str, ...]):
    """Insert el right after the last child of n whose tag is in tags (MusicXML order matters)."""
    pos = [i for i, c in enumerate(n) if c.tag in tags]
    n.insert(pos[-1] + 1 if pos else len(n), el)


def _repair_rhythm(m: ET.Element, expected: int):
    """Small fixes that make a voice fill its bar exactly; each applied only when it is
    the single change that does so."""
    for notes in _voices(m).values():
        total = _voice_end(notes)
        if total == expected:
            continue
        last = notes[-1][1]
        # A whole-bar rest keeps the bar length Audiveris guessed before other repairs.
        rest = last.find("rest")
        if len(notes) == 1 and rest is not None and (rest.get("measure") == "yes"
                                                     or last.findtext("type") in (None, "whole")):
            last.find("duration").text = str(expected)
            _grow_backup_after(m, last, expected - total)
            continue
        if total < expected:
            # A tuplet mark Audiveris invented (e.g. the "3" of "Play 3 times").
            group, groups = [], []
            for _, n in notes:
                tm = n.find("time-modification")
                if tm is not None:
                    group.append(n)
                if group and (tm is None or n.find(".//tuplet[@type='stop']") is not None):
                    groups.append(group)
                    group = []
            for g in groups + ([group] if group else []):
                tm = g[0].find("time-modification")
                actual, normal = int(tm.findtext("actual-notes")), int(tm.findtext("normal-notes"))
                gain = sum(int(n.findtext("duration")) * (actual - normal) // normal for n in g)
                if total + gain == expected:
                    for n in g:
                        for c in _chord_of(m, n):
                            d = c.find("duration")
                            d.text = str(int(d.text) * actual // normal)
                            c.remove(c.find("time-modification"))
                            for nots in c.findall("notations"):
                                for t in nots.findall("tuplet"):
                                    nots.remove(t)
                                if len(nots) == 0:
                                    c.remove(nots)
                    _grow_backup_after(m, last, gain)
                    total += gain
                    break
        if total != expected:
            # A lost or extra augmentation dot (dot = +half the note's value).
            diff = expected - total
            if diff > 0:
                cands = [n for _, n in notes if not n.findall("dot") and n.find("time-modification") is None
                         and int(n.findtext("duration")) == 2 * diff]
            else:
                cands = [n for _, n in notes if len(n.findall("dot")) == 1
                         and int(n.findtext("duration")) == -3 * diff]
            if len(cands) == 1:
                for c in _chord_of(m, cands[0]):
                    d = c.find("duration")
                    d.text = str(int(d.text) + diff)
                    if diff > 0:
                        _insert_after(c, ET.Element("dot"), ("type", "dot"))
                    else:
                        c.remove(c.find("dot"))
                _grow_backup_after(m, last, diff)


# ------------------------------------------------------------------ notes

def _clef_steps(part: ET.Element, measure: int) -> dict[int, int]:
    """Staff number -> diatonic step number (octave*7 + step) of the staff's middle line."""
    clefs = {}
    for m in part.findall("measure")[:measure + 1]:
        for c in m.iter("clef"):
            clefs[int(c.get("number", "1"))] = (c.findtext("sign"), int(c.findtext("line", "2")),
                                                 int(c.findtext("clef-octave-change", "0")))
    out = {}
    for staff, (sign, line, octv) in clefs.items():
        ref = {"G": 4 * 7 + 4, "F": 3 * 7 + 3, "C": 4 * 7 + 0}.get(sign)  # G4, F3, C4 sit on `line`
        if ref is not None:
            out[staff] = ref + 2 * (3 - line) + 7 * octv
    return out


def _step_number(note: ET.Element) -> int | None:
    p = note.find("pitch")
    if p is None:
        return None
    return int(p.findtext("octave")) * 7 + STEPS.index(p.findtext("step"))


def _accidental_glyph(page: np.ndarray, low: Head, high: Head) -> str | None:
    """What a 'notehead' glued to the left of a real note actually is: flat, sharp or natural.
    Judged by its vertical strokes: a flat has one stem rising well above its bowl; a sharp
    has two reaching above and below; a natural two, the left one higher."""
    x, y, w, h = low.box
    il = h  # a notehead is about one staff space tall
    x0, x1 = max(int(x - 0.4 * il), 0), high.box[0] - 2
    y0, y1 = max(int(y - 2.5 * il), 0), int(y + h + 1.5 * il)
    win = page[y0:y1, x0:x1] < 128
    if win.size == 0:
        return None
    win = ndimage.binary_closing(win, np.ones((5, 1)))  # bridge staff-line gaps in stems
    strokes = []
    for cx in range(win.shape[1]):
        col = np.concatenate([[0], win[:, cx].astype(int), [0]])
        e = np.flatnonzero(np.diff(col))
        runs = list(zip(e[::2], e[1::2]))
        top, bot = max(runs, key=lambda r: r[1] - r[0], default=(0, 0))
        if bot - top >= 1.5 * il:
            if strokes and strokes[-1][1] == cx - 1:
                strokes[-1][1:] = [cx, min(strokes[-1][2], top), max(strokes[-1][3], bot)]
            else:
                strokes.append([cx, cx, top, bot])
    head_top, head_bot = y - y0, y + h - y0
    if len(strokes) == 1:
        _, _, top, bot = strokes[0]
        if top < head_top - 0.8 * il and abs(bot - head_bot) < 0.6 * il:
            return "flat"
    if len(strokes) == 2:
        (_, _, t1, b1), (_, _, t2, b2) = strokes
        if t1 < head_top and t2 < head_top and b1 > head_bot and b2 > head_bot and abs(t1 - t2) < 0.4 * il:
            return "sharp"
        if t1 < t2 - 0.4 * il and b2 > b1 + 0.4 * il:
            return "natural"
    return None


def _fix_glued_accidentals(score: ET.Element, book: Book) -> set[int]:
    """Audiveris sometimes reads an accidental touching its note as a second notehead
    (D-flat -> a C+D chord). Such a 'head' has a very low grade and hugs a confident head
    on the same or next staff step. Returns the ids of heads explained this way."""
    used = set()
    parts = score.findall("part")
    for low in book.heads:
        if low.grade >= 0.3:
            continue
        x, y, w, h = low.box
        high = next((r for r in book.heads if r.sheet == low.sheet and r.part == low.part
                     and r.staff == low.staff and r.measure == low.measure and r.grade >= 0.4
                     and abs(r.pitch - low.pitch) <= 1 and 0 < r.box[0] - x <= 1.6 * w
                     and abs(r.box[1] - y) < h), None)
        if high is None or low.part >= len(parts):
            continue
        acc = _accidental_glyph(book.pages[low.sheet - 1], low, high)
        part = parts[low.part]
        measures = part.findall("measure")
        if acc is None or low.measure >= len(measures):
            continue
        middle = _clef_steps(part, low.measure).get(low.staff)
        if middle is None:
            continue
        m = measures[low.measure]
        on_staff = [n for n in m.findall("note") if int(n.findtext("staff", "1")) == low.staff]
        ln = next((n for n in on_staff if _step_number(n) == middle - low.pitch), None)
        hn = next((n for n in on_staff if _step_number(n) == middle - high.pitch and n is not ln), None)
        if ln is None or hn is None:
            continue
        head = ln if ln.find("chord") is None else hn if hn.find("chord") is None else None
        chord = _chord_of(m, head) if head is not None else []
        if ln not in chord or hn not in chord:
            continue  # not one chord: leave it alone
        if ln is head:  # keep the element carrying beams/lyrics; give it the real pitch
            old = ln.find("pitch")
            idx = list(ln).index(old)
            ln.remove(old)
            ln.insert(idx, hn.find("pitch"))
            m.remove(hn)
            keep = ln
        else:
            m.remove(ln)
            keep = hn
        pitch = keep.find("pitch")
        for al in pitch.findall("alter"):
            pitch.remove(al)
        alter = {"flat": -1, "sharp": 1, "natural": 0}[acc]
        if alter:
            al = ET.Element("alter")
            al.text = str(alter)
            pitch.insert(1, al)
        for old in keep.findall("accidental"):
            keep.remove(old)
        el = ET.Element("accidental")
        el.text = acc
        _insert_after(keep, el, ("type", "dot"))
        used.add(id(low))
    return used


def _unsure_measures(book: Book, explained: set[int]) -> set[tuple[int, int]]:
    """(part, measure) pairs where Audiveris itself doubted many of the noteheads."""
    stats: dict[tuple[int, int], list[int]] = {}
    for hd in book.heads:
        if id(hd) in explained:
            continue
        s = stats.setdefault((hd.part, hd.measure), [0, 0])
        s[0] += 1
        s[1] += hd.grade < LOW_GRADE
    return {k for k, (n, low) in stats.items() if low >= 2 and low >= 0.3 * n}


# ------------------------------------------------------------------ flagging

def _flag(m: ET.Element, expected: int, unsure: bool = False) -> bool:
    """Color every note of a measure whose voices don't fill the time signature,
    or whose noteheads Audiveris itself was unsure of."""
    if m.get("implicit") == "yes" and not unsure:
        return False
    bad = unsure or any(_voice_end(notes) != expected for notes in _voices(m).values())
    if bad:
        for n in m.iter("note"):
            n.set("color", FLAG_COLOR)
    return bad


# ------------------------------------------------------------------ text

def _clean(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)          # "ﬂy" -> "fly"
    return re.sub(r"(?<=[A-Za-z])0(?=[A-Za-z]|\b)", "o", s)  # OCR "G00d" -> "Good"


def _fix_text(score: ET.Element):
    for tag in ("text", "credit-words", "words", "work-title", "movement-title"):
        for el in score.iter(tag):
            if el.text:
                el.text = _clean(_clean(el.text))


def _fix_credits(score: ET.Element):
    """Audiveris files every stray text (chord names, page numbers, tuplet numbers)
    as a page credit. Keep the title and the byline from page 1, drop the rest.
    "Original key ..." is dropped too: it is wrong once transposed."""
    page1 = []
    for c in score.findall("credit"):
        w = c.find("credit-words")
        if c.get("page", "1") == "1" and w is not None and w.text:
            page1.append((float(w.get("font-size", 0)), w))
        score.remove(c)
    page1.sort(key=lambda fw: -fw[0])
    if not page1:
        return
    title = page1[0][1]
    byline = next((w for _, w in page1[1:] if re.search(r"\b(by|words|music|arr)", w.text, re.I)), None)
    work = score.find("work")
    if work is None:
        work = ET.Element("work")
        score.insert(0, work)
    if work.find("work-title") is None:
        ET.SubElement(work, "work-title").text = title.text
    credits = [("title", title)] + ([("subtitle", byline)] if byline is not None else [])
    for kind, w in reversed(credits):
        c = ET.Element("credit", {"page": "1"})
        ET.SubElement(c, "credit-type").text = kind
        w.attrib.update({"justify": "left", "valign": "bottom"})  # Audiveris gives the left baseline
        w.text = w.text.replace("&", "&amp;")  # MuseScore parses credits as rich text
        c.append(w)
        score.insert(list(score).index(score.find("part-list")), c)
    # Chord names Audiveris kept as plain text; real chord symbols replace them.
    for m in score.iter("measure"):
        for d in m.findall("direction"):
            words = [w.text or "" for w in d.iter("words")]
            if words and all(_is_chordlike(t) for t in words):
                m.remove(d)


def _drop_text_dynamics(score: ET.Element):
    """Audiveris reads letters as dynamics: the f of "for" on the lyric line, or bits of
    the next system's chord names far below/above a staff. Real dynamics sit near the staff.
    default-y is in tenths from the staff's top line (a staff is 40 tenths tall)."""
    for part in score.findall("part"):
        lyric_y = [float(l.get("default-y")) for l in part.iter("lyric") if l.get("default-y")]
        lyric_y = sorted(lyric_y)[len(lyric_y) // 2] if lyric_y else None
        staves = int(part.findtext(".//attributes/staves", "1"))
        for m in part.findall("measure"):
            for d in m.findall("direction"):
                dyn = d.find(".//dynamics")
                if dyn is None or len(list(d.find("direction-type"))) != 1 or dyn.get("default-y") is None:
                    continue
                y, staff = float(dyn.get("default-y")), int(d.findtext("staff", "1"))
                on_lyrics = lyric_y is not None and staff == 1 and abs(y - lyric_y) <= 12
                if on_lyrics or (staff == staves and y < -80) or (staff == 1 and y > 60):
                    m.remove(d)


DYNAMICS = {"DYNAMICS_" + k.upper(): k for k in ("p", "pp", "ppp", "mp", "mf", "f", "ff", "fff", "sf", "sfz", "fp", "rf", "rfz", "fz", "sfp")}


def _inside(inner: tuple, outer: tuple, slack: int = 3) -> bool:
    cx, cy = inner[0] + inner[2] / 2, inner[1] + inner[3] / 2
    return (outer[0] - slack <= cx <= outer[0] + outer[2] + slack
            and outer[1] - slack <= cy <= outer[1] + outer[3] + slack)


def _drop_symbols_in_text(score: ET.Element, book: Book):
    """A dynamics sign Audiveris found inside a line of text is a letter of that text
    (the "p" in "Play 3 times ad lib.")."""
    parts = score.findall("part")
    for d in book.dynamics:
        if d.measure is None or not any(t.sheet == d.sheet and _inside(d.box, t.box) for t in book.texts):
            continue
        for part in [parts[d.part]] if d.part is not None and d.part < len(parts) else parts:
            measures = part.findall("measure")
            if d.measure >= len(measures):
                continue
            m = measures[d.measure]
            hit = next((el for el in m.findall("direction") if el.find(".//dynamics") is not None
                        and any(c.tag == DYNAMICS.get(d.kind) for c in el.find(".//dynamics"))), None)
            if hit is not None:
                m.remove(hit)
                break


def _reread_directions(score: ET.Element, book: Book):
    """Read text directions ("Play 3 times ad lib.") again with the accurate OCR model:
    Audiveris' reading loses words when it mistakes some for symbols."""
    parts = score.findall("part")
    for pi, part in enumerate(parts):
        for mi, m in enumerate(part.findall("measure")):
            for w in m.iter("words"):
                old = (w.text or "").strip()
                if len(old) < 3:
                    continue
                marks = [t for t in book.texts if t.measure == mi and t.part == pi and t.text]
                best = max(marks, key=lambda t: difflib.SequenceMatcher(None, t.text, old).ratio(), default=None)
                if best is None or difflib.SequenceMatcher(None, best.text, old).ratio() < 0.6:
                    continue
                new = lyrics.ocr_box(book, best.sheet, best.box)
                if (len(new.split()) >= len(old.split())
                        and difflib.SequenceMatcher(None, new.lower(), old.lower()).ratio() >= 0.6):
                    w.text = new


def _is_chordlike(text: str) -> bool:
    t = text.strip()
    return bool(re.fullmatch(r"N\.?C\.?", t) or CHORD_RE.match(t))


# ------------------------------------------------------------------ harmony

STEPS = "CDEFGAB"
KINDS = {  # suffix (quality + plain extension) -> MusicXML kind
    "": "major", "m": "minor", "min": "minor", "7": "dominant", "maj7": "major-seventh",
    "m7": "minor-seventh", "dim": "diminished", "o": "diminished", "dim7": "diminished-seventh",
    "o7": "diminished-seventh", "aug": "augmented", "+": "augmented", "7aug": "augmented-seventh",
    "aug7": "augmented-seventh", "+7": "augmented-seventh", "m7b5": "half-diminished",
    "6": "major-sixth", "m6": "minor-sixth", "9": "dominant-ninth", "maj9": "major-ninth",
    "m9": "minor-ninth", "11": "dominant-11th", "m11": "minor-11th", "13": "dominant-13th",
    "maj13": "major-13th", "m13": "minor-13th", "sus4": "suspended-fourth", "sus": "suspended-fourth",
    "sus2": "suspended-second", "5": "power", "69": "major-sixth", "6/9": "major-sixth",
}


WHOLE_KINDS = {"m7b5": "half-diminished", "7aug": "augmented-seventh", "aug7": "augmented-seventh",
               "+7": "augmented-seventh", "7#5": "augmented-seventh", "dim7": "diminished-seventh",
               "o7": "diminished-seventh", "mmaj7": "major-minor"}


def _alter(acc: str) -> int:
    return {"#": 1, "b": -1}.get(acc, 0)


def harmony(text: str) -> ET.Element:
    h = ET.Element("harmony", {"print-frame": "no"})
    if text == "N.C.":
        r = ET.SubElement(h, "root")
        ET.SubElement(r, "root-step", {"text": ""}).text = "C"
        ET.SubElement(h, "kind", {"text": "N.C."}).text = "none"
        return h
    m = CHORD_RE.match(text)
    r = ET.SubElement(h, "root")
    ET.SubElement(r, "root-step").text = m.group("r")
    if m.group("a"):
        ET.SubElement(r, "root-alter").text = str(_alter(m.group("a")))
    q, e = m.group("q") or "", m.group("e") or ""
    suffix = q + e
    if suffix in WHOLE_KINDS:  # one MusicXML kind covers the whole suffix
        kind, text, degrees = WHOLE_KINDS[suffix], suffix, []
    else:
        # Plain part -> kind; altered/added tones (b9, #11, add9) -> <degree>. The kind text
        # holds only the plain part: MuseScore prints text + degrees when it has no exact
        # chord-list match, so a full "7b9" text would come out as "7b9b9".
        plain = re.match(r"(?:6/?9|13|11|9|7|6|5|4|2)?(?:aug|sus[24]?)?", e).group(0)
        kind, text = KINDS.get(q + plain) or KINDS.get(q) or "major", q + plain
        degrees = re.findall(r"([#b]|add)(\d+)", e[len(plain):])
        if plain in ("69", "6/9"):
            degrees.append(("add", "9"))
    ET.SubElement(h, "kind", {"text": text}).text = kind
    if m.group("br"):
        b = ET.SubElement(h, "bass")
        ET.SubElement(b, "bass-step").text = m.group("br")
        if m.group("ba"):
            ET.SubElement(b, "bass-alter").text = str(_alter(m.group("ba")))
    for acc, num in degrees:
        d = ET.SubElement(h, "degree")
        ET.SubElement(d, "degree-value").text = num
        ET.SubElement(d, "degree-alter").text = str(_alter(acc))
        ET.SubElement(d, "degree-type").text = "alter" if num == "5" and acc != "add" else "add"
    return h


def _set_harmony(score: ET.Element, chords: list[Chord]):
    for part in score.findall("part"):
        for m in part.findall("measure"):
            for h in m.findall("harmony"):
                m.remove(h)
    if not chords:
        return
    part = score.findall("part")[0]
    measures = part.findall("measure")
    state = {"div": 1, "time": Fraction(3, 4)}
    divs = []
    for m in measures:
        _measure_len(m, state)
        divs.append(state["div"])
    for c in sorted(chords, key=lambda c: (c.measure, c.offset), reverse=True):
        if not 0 <= c.measure < len(measures):
            continue
        m = measures[c.measure]
        at = int(c.offset * 4 * divs[c.measure])
        voices = _voices(m)
        if not voices:
            continue
        notes = voices[min(voices)]  # top staff, first voice
        onset, note = next(((o, n) for o, n in notes if o >= at), notes[-1])
        h = harmony(c.text)
        if onset != at:
            ET.SubElement(h, "offset").text = str(at - onset)
        m.insert(list(m).index(note), h)
