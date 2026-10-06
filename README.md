# musicPDF

Put in a sheet-music PDF and choose what to make from it:

- **Transpose**: a newly engraved PDF in another key (with chord symbols and
  lyrics), plus a MuseScore file you can edit. Pick the key, or a number of
  semitones.
- **Practice tracks**: plain-piano MP3s of the whole piece or just its main line
  (the vocal or solo part), at any speed.
- or **both**: the tracks are then in the new key.

```
./musicpdf.py song.pdf --key Am --direction down    # -> song_-7.pdf, song_-7.mscz
./musicpdf.py song.pdf --semitones 3                # -> song_+3.pdf
./musicpdf.py song.pdf --track both --speed 80      # -> song_full_80pct.mp3, song_main_80pct.mp3
./musicpdf.py song.pdf --key Bb --track main        # transposed PDF + its main-line track
./musicpdf.py --serve                               # web page at http://localhost:8771
```

## Transposing

`--key` takes a key name: `Am`, `Bb`, `F# major`, `E♭ minor`. Relative major and
minor keys share a key signature, so `Am` and `C` give the same result; the web
page lists keys as pairs ("C major / A minor"). `--direction` picks whether the
music moves `up`, `down`, or whichever is `closest` (default). Or give
`--semitones N` (-12..12) instead. The new key is spelled with the fewest sharps
or flats.

## Practice tracks

The score is played back as plain piano (`--track full|main|both`):

- **whole piece** (`full`): every part, voice and accompaniment, on piano;
- **main line** (`main`): only the solo line above the accompaniment (a song's
  vocal line, a violin over piano); for a single-part score, the top notes of the
  upper staff.

`--speed` sets the tempo in percent (e.g. 75 to practice slower). Tempo marks
read from the page ("♩. = 80") and repeat counts ("Play 3 times") are honored;
chord symbols are not played. On the web page, the result also has buttons to
make more tracks at other speeds, with a player.

## Setup

Linux x86_64 (tested on Ubuntu 24.04). Needs `python3` (with venv), `curl`,
`unzip` and `pdftoppm` (`sudo apt install python3-venv curl unzip poppler-utils`).

```
git clone https://github.com/Tennys0nmiles/musicPDF.git
cd musicPDF
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
4. **Repair the MusicXML** (`fixup.py`, `lyrics.py`, `octaves.py`):
   - chord symbols restored;
   - an accidental touching its note, which Audiveris reads as an extra
     notehead (D♭ → a C+D chord), turned back into the accidental;
   - rhythm: quadruplets in 6/8, invented triplets (the "3" of "Play 3 times"),
     lost or extra dots, whole-bar rests — each applied only when it makes the
     bar add up exactly;
   - octave (8va / "(8)") lines found on the page and applied to the notes
     under them;
   - lyrics: held-syllable lines vs hyphens told apart by height, junk removed
     (dashed lines read as text), words not in the dictionary re-read from the
     page ("mme" → "mine"), and words Audiveris missed recovered from the line;
   - "dynamics" and tuplet numbers that are really letters of nearby text dropped,
     and text directions re-read.
5. **Transpose and engrave** with MuseScore (`engrave.py`), picking the key
   signature with the fewest sharps/flats.
6. **Practice tracks** (`audio.py`): a playback copy of the score with every part
   on piano and no chord-symbol playback (optionally only the main line) is
   transposed the same way and rendered to MP3 with MuseScore's built-in sounds.

## Accuracy

Optical music recognition is not perfect, so check the output against the
original. Bars the reader was unsure of are **printed in red** and listed when
the run finishes: their rhythm doesn't add up, or Audiveris itself had low
confidence in their noteheads. Fix those in the `.mscz` file (open it in
MuseScore), or use `--keep DIR` to get the repaired, untransposed
`score.musicxml` to edit.

Known weak spot: small cue-size notes far above the staff on many ledger lines
(e.g. a brass line written into the vocal staff) — Audiveris misreads them; they
are flagged red. Clean printed scores work best; phone photos and handwriting
won't.

The lyric spell-check uses the system word list (`/usr/share/dict/words`; on
Ubuntu `sudo apt install wamerican`); without one that step is skipped.

A full run takes roughly 20 seconds per page.
