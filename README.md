# transposition

Transpose sheet music from a PDF by any number of semitones. Drop in a PDF,
pick +2 or -3, and get back a newly engraved PDF in the new key, with chord
symbols and lyrics, plus a MuseScore file you can edit.

```
./transpose.py song.pdf 2            # -> song_+2.pdf and song_+2.mscz
./transpose.py song.pdf -3 -o out.pdf
./transpose.py --serve               # web page at http://localhost:8771
```

## Setup

Linux x86_64 (tested on Ubuntu 24.04). Needs `python3` (with venv), `curl`,
`unzip` and `pdftoppm` (`sudo apt install python3-venv curl unzip poppler-utils`).

```
git clone https://github.com/Tennys0nmiles/transposition.git
cd transposition
./setup.sh
```

`setup.sh` downloads the programs this tool drives into `vendor/` (about 400 MB
of downloads, 800 MB unpacked; nothing is installed system-wide) and makes a
Python venv:

| Tool | Used for | License |
| --- | --- | --- |
| [Audiveris](https://github.com/Audiveris/audiveris) 5.11 | reading notes off the page (OMR) | AGPL-3.0 |
| [MuseScore Studio](https://musescore.org) 4.7 | transposing and engraving | GPL-3.0 |
| [Tesseract](https://github.com/tesseract-ocr/tesseract) 5.5 (from Audiveris) | reading chord names | Apache-2.0 |

## How it works

1. **Clean the page** (`clean.py`). Guitar chord diagrams are erased before
   recognition: Audiveris reads their "3fr" labels as triplets and they get in
   the way of the chord names.
2. **Recognize the music** (`omr.py`). Audiveris turns the page images into
   MusicXML; its project file also gives every bar's position on the page.
3. **Read chord symbols** (`chords.py`). Audiveris misses most chord names with
   sharps, flats or superscripts (F♯m/C♯, B♭7♭9, Cmaj⁷), so the strip above each
   vocal line is OCR'd separately: superscripts are enlarged, each name is read
   8 ways, and identical-looking names across the piece pool their readings.
4. **Repair the MusicXML** (`fixup.py`): restore the chord symbols, turn
   hyphens that were really held-syllable lines ("high,\_\_\_") back into
   extenders, rebuild quadruplets in 6/8, drop "dynamics" that were misread
   lyric letters, and clean up the title.
5. **Transpose and engrave** with MuseScore, picking the key signature with the
   fewest sharps/flats.

## Accuracy

Optical music recognition is not perfect, so check the output against the
original. Any bar whose rhythm still doesn't add up is **printed in red** and
listed when the run finishes. Fix those in the `.mscz` file (open it in MuseScore).

Known weak spots: notes far above the staff on many ledger lines, octave (8va)
lines, and lyrics that run into other markings. Clean printed scores work best;
phone photos and handwriting won't.

A full run takes roughly 20 seconds per page.
