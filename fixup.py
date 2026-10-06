"""Repair Audiveris MusicXML before engraving.

- chord symbols: replace Audiveris' few <harmony> with the ones read by chords.py
- lyrics: a melisma line after "high,___" is an extender, not a hyphen
- rhythm: quadruplets in compound meter (4 eighths in a dotted quarter), whose "4" Audiveris ignores
- dynamics that are really misread text (lyric letters, chord names) are dropped
- flag measures whose voices still don't add up (colored red, reported back)
"""
import difflib, re, unicodedata, zipfile
import xml.etree.ElementTree as ET
from fractions import Fraction

from chords import Chord, CHORD_RE
from omr import Book

FLAG_COLOR = "#E0201B"
SCALE = 4  # durations are multiplied so quadruplet eighths stay integral


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
    _fix_extenders(score, book)
    _drop_text_dynamics(score)
    for part in score.findall("part"):
        state = {"div": 1, "time": Fraction(3, 4)}
        for m in part.findall("measure"):
            _repair_tuplets(m, _measure_len(m, state), state.get("meter", (3, 4)))
    _set_harmony(score, chords)
    flagged = set()
    for part in score.findall("part"):
        state = {"div": 1, "time": Fraction(3, 4)}
        for i, m in enumerate(part.findall("measure")):
            if _flag(m, _measure_len(m, state)):
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
                _shrink_backup_after(m, group[-1], dur)
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


def _shrink_backup_after(m: ET.Element, last: ET.Element, amount: int):
    """The voice got shorter; the <backup> that rewinds to the measure start must too."""
    children = list(m)
    for el in children[children.index(last) + 1:]:
        if el.tag == "backup":
            d = el.find("duration")
            d.text = str(int(d.text) - amount)
            return
        if el.tag == "forward":
            return


# ------------------------------------------------------------------ flagging

def _flag(m: ET.Element, expected: int) -> bool:
    """Color every note of a measure whose voices don't fill the time signature."""
    if m.get("implicit") == "yes":
        return False
    bad = any(_voice_end(notes) != expected for notes in _voices(m).values())
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


def _is_chordlike(text: str) -> bool:
    t = text.strip()
    return bool(re.fullmatch(r"N\.?C\.?", t) or CHORD_RE.match(t))


def _fix_extenders(score: ET.Element, book: Book):
    """Audiveris files the "___" after a held syllable as a hyphen. Re-read which
    connector follows each syllable from its geometry and fix <syllabic>."""
    seq = []  # (text, followed_by_extender) in reading order
    by_line: dict[tuple, list] = {}
    for li in book.lyrics:
        il = book.systems[li.system].interline if 0 <= li.system < len(book.systems) else 20
        by_line.setdefault((li.sheet, li.system, round(li.y / il)), []).append(li)
    for key in sorted(by_line):
        items = sorted(by_line[key], key=lambda li: li.x)
        for i, li in enumerate(items):
            if li.kind == "Syllable":
                nxt = items[i + 1] if i + 1 < len(items) else None
                seq.append((li.value, nxt is not None and nxt.kind != "Syllable" and "_" in nxt.value))
    lyrics = [ly for part in score.findall("part") for ly in part.iter("lyric") if ly.find("text") is not None]
    a = [unicodedata.normalize("NFKC", ly.findtext("text")) for ly in lyrics]
    b = [t for t, _ in seq]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            ly, (_, ext) = lyrics[blk.a + k], seq[blk.b + k]
            syl = ly.find("syllabic")
            if not ext or syl is None or syl.text not in ("begin", "middle"):
                continue
            syl.text = "single" if syl.text == "begin" else "end"
            if ly.find("extend") is None:
                ly.insert(list(ly).index(ly.find("text")) + 1, ET.Element("extend"))
            if blk.a + k + 1 < len(lyrics):
                nsyl = lyrics[blk.a + k + 1].find("syllabic")
                if nsyl is not None and nsyl.text in ("end", "middle"):
                    nsyl.text = "single" if nsyl.text == "end" else "begin"


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
