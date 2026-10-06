"""Octave lines (8va / 15ma, and the "(8)" continuation on later lines).

Audiveris finds few of them and exports them as unterminated starts on notes left
at written pitch, which MuseScore drops: the music then reads an octave low. We
find the dashed lines on the page ourselves and rewrite the affected notes at
sounding pitch inside proper start/stop markings (MusicXML's convention).
"""
import os, re, subprocess, tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

from omr import Book, System

TESS = Path(__file__).resolve().parent / "vendor/tesseract"


@dataclass
class Ottava:
    system: int      # index into Book.systems
    staff: int       # index into system.staves (the staff the line is above)
    x0: int
    x1: int
    size: int        # 8 or 15
    hooked: bool     # ends with a downward hook (else it runs on to the next line)


# Other markings drawn with a dashed line; an octave label never reads as these.
DASHED_WORDS = re.compile(r"rit|rall|accel|cresc|dim|decresc|poco|molto|smorz|morendo|string|allarg|sost|ten|pedal|ped",
                          re.I)


def _label(page: np.ndarray, start: int, cy: int, il: float) -> str | None:
    """The label just left of a dashed line's first dash, as text (possibly garbled:
    italic "8va"/"(8)" OCR poorly), or None when there is no label-sized mark there."""
    x0, y0, x1, y1 = int(start - 3.5 * il), int(cy - 2.2 * il), int(start), int(cy + 1.4 * il)
    region = page[max(y0, 0):y1, max(x0, 0):x1] < 128
    if region.size == 0:
        return None
    # Only blobs wholly inside the region (the label), not bits of notes or stems.
    lab, _ = ndimage.label(region)
    objs = ndimage.find_objects(lab)
    keep = [i + 1 for i, sl in enumerate(objs)
            if sl[0].start > 0 and sl[0].stop < region.shape[0] and sl[1].start > 0
            and sl[0].stop - sl[0].start > 0.3 * il]
    if not keep:
        return None
    ya = min(objs[i - 1][0].start for i in keep); yb = max(objs[i - 1][0].stop for i in keep)
    xa = min(objs[i - 1][1].start for i in keep); xb = max(objs[i - 1][1].stop for i in keep)
    if not (0.8 * il <= yb - ya <= 2.5 * il and il <= xb - xa <= 4 * il) or region.shape[1] - xb > 0.8 * il:
        return None  # not a label-sized mark right at the line's start
    crop = np.where(np.isin(lab[ya:yb, xa:xb], keep), 0, 255).astype(np.uint8)
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "l.png"
        img = Image.fromarray(np.pad(crop, 8, constant_values=255))
        img.resize((img.width * 3, img.height * 3)).save(f)
        env = dict(os.environ, LD_LIBRARY_PATH=str(TESS), TESSDATA_PREFIX=str(TESS / "tessdata_best"),
                   OMP_THREAD_LIMIT="1")
        return subprocess.run([str(TESS / "tesseract"), str(f), "-", "--psm", "7"],
                              capture_output=True, text=True, env=env).stdout.strip()


def find(book: Book) -> list[Ottava]:
    """Long dashed lines just above a staff that start with a short label (8va, (8), 15ma)."""
    found = []
    for si, system in enumerate(book.systems):
        page = book.pages[system.sheet - 1]
        il = system.interline
        right_edge = system.stacks[-1].right
        for k, (top, _) in enumerate(system.staves):
            y0, y1 = int(top - 4 * il), int(top - 0.2 * il)
            if k > 0:
                y0 = max(y0, int(system.staves[k - 1][1] + 0.5 * il))
            if y1 - y0 < il:
                continue
            band = page[y0:y1] < 128
            lab, _ = ndimage.label(band)
            dashes = []
            for sl in ndimage.find_objects(lab):
                h, w = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
                if h <= 0.3 * il and 0.25 * il <= w <= 1.5 * il:
                    dashes.append(((sl[0].start + sl[0].stop) / 2, sl[1].start, sl[1].stop))
            dashes.sort(key=lambda d: d[1])
            # Chain dashes left to right, each at nearly the height of the previous one (scans
            # are tilted), across short gaps (a stem may cross the line and swallow a dash).
            used = set()
            for i, (y, xa, xb) in enumerate(dashes):
                if i in used:
                    continue
                chain = [i]
                for j in range(i + 1, len(dashes)):
                    yj, xj, xk = dashes[j]
                    if j not in used and abs(yj - dashes[chain[-1]][0]) <= 0.15 * il \
                            and 0 < xj - dashes[chain[-1]][2] <= 2.5 * il:
                        chain.append(j)
                if len(chain) < 10:
                    continue
                used.update(chain)
                cy = int(y0 + y)
                start, end = dashes[chain[0]][1], dashes[chain[-1]][2]
                label = _label(page, int(start), cy, il)
                if label is None or DASHED_WORDS.search(label):
                    continue
                # A hook: ink going straight down from the line's end.
                hook = page[cy:int(cy + 1.2 * il), max(int(end - 0.2 * il), 0):int(end + 0.4 * il)] < 128
                hooked = hook.size > 0 and hook.any(1).sum() > 0.6 * il
                found.append(Ottava(si, k, int(start - 2 * il), min(int(end), right_edge),
                                    15 if re.search(r"15|ma", label) else 8, hooked or end < right_edge - 1.5 * il))
    return found


def _span(system: System, x0: int, x1: int) -> tuple[tuple[int, Fraction], tuple[int, Fraction]]:
    """(measure, onset) of the first and last slot under [x0, x1]."""
    slots = [(st.measure, off, x) for st in system.stacks for x, off in st.slots]
    inside = [s for s in slots if x0 <= s[2] <= x1] or slots
    return (inside[0][0], inside[0][1]), (inside[-1][0], inside[-1][1])


def apply(score: ET.Element, book: Book, ottavas: list[Ottava], staff_part) -> int:
    """Rewrite notes under each octave line at sounding pitch with start/stop markings.
    `staff_part(system, staff index)` -> (part index, staff number) or None."""
    parts = score.findall("part")
    # Audiveris' own octave-shift markings are incomplete; ours replace them.
    for part in parts:
        for m in part.iter("measure"):
            for d in m.findall("direction"):
                if d.find(".//octave-shift") is not None:
                    m.remove(d)
    done = 0
    for o in ottavas:
        system = book.systems[o.system]
        where = staff_part(system, o.staff)
        if where is None:
            continue
        pi, staff = where
        measures = parts[pi].findall("measure")
        (m0, t0), (m1, t1) = _span(system, o.x0, o.x1)
        shift = 1 if o.size == 8 else 2
        first = last = None
        for mi in range(m0, m1 + 1):
            m, pos, onset, div = measures[mi], 0, 0, None
            for el in m:
                if el.tag == "attributes" and el.find("divisions") is not None:
                    div = int(el.findtext("divisions"))
                if el.tag == "backup":
                    pos -= int(el.findtext("duration"))
                elif el.tag == "forward":
                    pos += int(el.findtext("duration"))
                elif el.tag == "note":
                    if el.find("chord") is None:  # chord tones share their head note's onset
                        onset = pos
                        if el.find("grace") is None:
                            pos += int(el.findtext("duration", "0"))
                    if int(el.findtext("staff", "1")) != staff:
                        continue
                    when = (mi, onset)
                    if not (_before_eq((m0, t0), when, measures, div) and _before_eq(when, (m1, t1), measures, div)):
                        continue
                    p = el.find("pitch")
                    if p is not None:
                        oc = p.find("octave")
                        oc.text = str(int(oc.text) + shift)
                    first = first or (mi, el)
                    last = (mi, el)
        if first is None:
            continue
        for kind, (mi, el) in (("start", first), ("stop", last)):
            m = measures[mi]
            d = ET.Element("direction", {"placement": "above"})
            dt = ET.SubElement(d, "direction-type")
            ET.SubElement(dt, "octave-shift", {"type": "down" if kind == "start" else "stop", "size": str(o.size)})
            ET.SubElement(d, "staff").text = str(staff)
            idx = list(m).index(el)
            if kind == "start":
                m.insert(idx, d)
            else:  # after the last affected note and its chord tones
                while idx + 1 < len(m) and m[idx + 1].tag == "note" and m[idx + 1].find("chord") is not None:
                    idx += 1
                m.insert(idx + 1, d)
        done += 1
    return done


def _before_eq(a, b, measures, div) -> bool:
    """Compare (measure, onset) positions; slot onsets are Fractions of a whole note,
    note onsets are divisions; normalize both to whole notes."""
    def whole(p):
        mi, t = p
        if isinstance(t, Fraction):
            return mi, t
        d = _divisions(measures, mi)
        return mi, Fraction(t, 4 * d)
    return whole(a) <= whole(b)


def _divisions(measures, mi) -> int:
    for m in reversed(measures[:mi + 1]):
        for a in m.findall("attributes"):
            if a.find("divisions") is not None:
                return int(a.findtext("divisions"))
    return 1
