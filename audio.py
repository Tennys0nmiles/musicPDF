"""Practice tracks: play the transposed score back as plain piano (MP3).

  full  every part (voice and accompaniment), all on piano
  main  only the main line: the solo part above the accompaniment (a song's
        vocal line, a violin over piano); for a single-part score, the top
        notes of the upper staff

Chord symbols are dropped first, or MuseScore would play them as extra chords.
"""
import copy, tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import engrave

MODES = ("full", "main")
STEPS = "CDEFGAB"


def _set_piano(score: ET.Element):
    for sp in score.iter("score-part"):
        name = sp.find("part-name")
        if name is not None:
            name.text = "Piano"  # MuseScore picks the sound from the instrument it recognizes
        for si in sp.findall("score-instrument"):
            for c in list(si):
                si.remove(c)
            ET.SubElement(si, "instrument-name").text = "Acoustic Grand Piano"
            ET.SubElement(si, "instrument-sound").text = "keyboard.piano"
        for mi in sp.findall("midi-instrument"):
            prog = mi.find("midi-program")
            if prog is None:
                prog = ET.SubElement(mi, "midi-program")
            prog.text = "1"


def _main_part(score: ET.Element) -> ET.Element:
    """The solo line: the first single-staff part (a voice or melody instrument above
    the accompaniment), else the first part."""
    parts = score.findall("part")
    for p in parts:
        if int(p.findtext(".//attributes/staves", "1")) == 1 and p.find(".//note/pitch") is not None:
            return p
    return parts[0]


def _pitch_number(note: ET.Element) -> int:
    p = note.find("pitch")
    return int(p.findtext("octave")) * 12 + [0, 2, 4, 5, 7, 9, 11][STEPS.index(p.findtext("step"))] \
        + int(float(p.findtext("alter", "0")))


def _to_rest(note: ET.Element):
    """Silence a note but keep its place in time."""
    keep = {"duration", "voice", "type", "dot", "time-modification", "staff", "chord", "grace"}
    for c in list(note):
        if c.tag not in keep:
            note.remove(c)
    note.insert(0, ET.Element("rest"))


def _melody_only(part: ET.Element):
    """Single-part score: keep the top note of the first voice on the upper staff."""
    for m in part.findall("measure"):
        notes = m.findall("note")
        voices = [n.findtext("voice", "1") for n in notes if n.findtext("staff", "1") == "1"]
        top_voice = min(voices, key=int) if voices else None
        group: list[ET.Element] = []

        def close(group):
            pitched = [n for n in group if n.find("pitch") is not None]
            if len(pitched) > 1:  # chord: give the head the highest pitch, drop the rest
                high = max(pitched, key=_pitch_number)
                head = group[0]
                head.remove(head.find("pitch"))
                head.insert(0, copy.deepcopy(high.find("pitch")))
                for n in group[1:]:
                    m.remove(n)

        for n in notes:
            if n.findtext("staff", "1") != "1" or n.findtext("voice", "1") != top_voice:
                if n.find("chord") is not None:
                    m.remove(n)  # a silenced chord's other tones go away
                else:
                    _to_rest(n)
                continue
            if n.find("chord") is None and group:
                close(group)
                group = []
            group.append(n)
        if group:
            close(group)


def _scale_tempo(score: ET.Element, speed: float):
    """Practice speed: every tempo times speed (percent); 120 bpm if the score sets none."""
    sounds = [s for s in score.iter("sound") if s.get("tempo")]
    first = score.find("part/measure")
    if first is not None and not any(s in list(first.iter("sound")) for s in sounds):
        d = ET.Element("direction")
        ET.SubElement(ET.SubElement(d, "direction-type"), "words").text = ""
        sounds.append(ET.SubElement(d, "sound", {"tempo": "120"}))
        first.insert(next((i for i, c in enumerate(first) if c.tag in ("note", "direction")), len(first)), d)
    for s in sounds:
        s.set("tempo", str(round(float(s.get("tempo")) * speed / 100, 2)))


def playback_score(xml: bytes, mode: str, speed: float = 100) -> bytes:
    """A copy of the score made for listening: all piano, no chord-symbol playback,
    for mode 'main' only the main line, at `speed` percent of the marked tempo."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if not 10 <= speed <= 300:
        raise ValueError("speed must be between 10 and 300 percent")
    score = ET.fromstring(xml)
    for m in score.iter("measure"):
        for h in m.findall("harmony"):
            m.remove(h)
    if mode == "main":
        parts = score.findall("part")
        main = _main_part(score)
        if len(parts) > 1:
            plist = score.find("part-list")
            for sp in plist.findall("score-part"):
                if sp.get("id") != main.get("id"):
                    plist.remove(sp)
            for el in plist.findall("part-group"):
                plist.remove(el)
            for p in parts:
                if p is not main:
                    score.remove(p)
        else:
            _melody_only(main)
    _set_piano(score)
    if speed != 100:
        _scale_tempo(score, speed)
    return ET.tostring(score, encoding="UTF-8", xml_declaration=True)


def render(xml: bytes, semitones: int, mode: str, fmt: str = "mp3", speed: float = 100) -> bytes:
    """Transpose the playback copy like the PDF and render it with MuseScore."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "playback.musicxml"
        src.write_bytes(playback_score(xml, mode, speed))
        if semitones:
            _, mscz = engrave.musescore(src, semitones, tmp)
            src = tmp / "playback.mscz"
            src.write_bytes(mscz)
        out = tmp / f"track.{fmt}"
        engrave.export(src, out)
        return out.read_bytes()
