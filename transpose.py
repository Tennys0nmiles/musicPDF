#!/usr/bin/env python3
"""Transpose sheet-music PDFs by N semitones.

PDF -> erase guitar chord diagrams -> Audiveris (OMR) -> chord-symbol OCR ->
MusicXML repairs -> MuseScore (transpose + engrave) -> PDF (+ editable .mscz).

Bars whose rhythm still doesn't add up after repair are printed in red and listed.

  ./transpose.py song.pdf 3          # up 3 semitones -> song_+3.pdf, song_+3.mscz
  ./transpose.py song.pdf -2 -o out.pdf
  ./transpose.py --serve             # web UI at http://localhost:8771
"""
import argparse, base64, json, os, secrets, subprocess, sys, tempfile, time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV_PY = HERE / ".venv/bin/python"
if VENV_PY.exists() and Path(sys.prefix).resolve() != (HERE / ".venv").resolve():
    os.execv(VENV_PY, [str(VENV_PY), *sys.argv])  # run inside the project venv made by setup.sh
sys.path.insert(0, str(HERE))
import chords, fixup, omr  # noqa: E402

MUSESCORE = HERE / "vendor/musescore/AppRun"
STYLE = HERE / "style.mss"


@dataclass
class Result:
    pdf: bytes
    mscz: bytes
    flagged: list[int] = field(default_factory=list)  # bar numbers to check by eye
    chords: int = 0


def target_key(fifths: int, semitones: int) -> int:
    """Key signature (in fifths) after transposing, spelled with the fewest accidentals."""
    pc = (fifths * 7 + semitones) % 12
    options = [f for f in range(-7, 8) if (f * 7) % 12 == pc]
    return min(options, key=lambda f: (abs(f), f > 0))


def first_key(xml: Path) -> int:
    fifths = ET.parse(xml).getroot().find(".//key/fifths")
    return int(fifths.text) if fifths is not None else 0


def musescore(xml: Path, semitones: int, out: Path) -> tuple[bytes, bytes]:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    if semitones == 0:
        pdf, mscz = out / "out.pdf", out / "out.mscz"
        for target in (pdf, mscz):
            subprocess.run([str(MUSESCORE), "-S", str(STYLE), "-o", str(target), str(xml)],
                           env=env, check=True, capture_output=True)
        return pdf.read_bytes(), mscz.read_bytes()
    if abs(semitones) == 12:
        opts = {"mode": "by_interval", "transposeInterval": 25}  # perfect octave
    else:
        opts = {"mode": "to_key", "targetKey": target_key(first_key(xml), semitones)}
    opts.update(direction="up" if semitones > 0 else "down", transposeKeySignatures=True,
                transposeChordNames=True, useDoubleSharpsFlats=False)
    proc = subprocess.run([str(MUSESCORE), "-S", str(STYLE), str(xml), "--score-transpose", json.dumps(opts)],
                          env=env, check=True, capture_output=True, text=True)
    data = json.loads(proc.stdout[proc.stdout.index("{"):])
    return base64.b64decode(data["pdf"]), base64.b64decode(data["mscz"])


def transpose(src: Path, semitones: int, log=print) -> Result:
    if not -12 <= semitones <= 12:
        raise ValueError("semitones must be between -12 and 12")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        if src.suffix.lower() in (".mxl", ".musicxml", ".xml"):
            xml, flagged, n = src, [], 0
        else:
            t = time.time()
            log("Reading the music (Audiveris)...")
            book = omr.run(src, tmp)
            log(f"  {len(book.pages)} pages in {time.time() - t:.0f}s. Reading chord symbols...")
            found = chords.detect(book)
            tree, flagged = fixup.fix(book, found)
            xml, n = tmp / "score.musicxml", len(found)
            tree.write(xml, encoding="UTF-8", xml_declaration=True)
        log("Engraving (MuseScore)...")
        pdf, mscz = musescore(xml, semitones, tmp)
    return Result(pdf, mscz, flagged, n)


# ---------------------------------------------------------------- web UI

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>Transposer</title>
<style>body{font:16px system-ui;max-width:32rem;margin:3rem auto;padding:0 1rem;line-height:1.5}
input,button{font:inherit;margin:.4rem 0;display:block}button{padding:.5rem 1rem}
.note{color:#555;font-size:.9rem}.warn{color:#b3261e}</style>
<h2>Transpose sheet music</h2>
%s
<form method=post enctype=multipart/form-data action=/transpose onsubmit="b.disabled=true;b.textContent='Working... (about 20 s per page)'">
<label>PDF (or MusicXML)<input type=file name=file accept=".pdf,.mxl,.musicxml,.xml" required></label>
<label>Semitones (+ up, &minus; down)<input type=number name=semitones value=0 min=-12 max=12></label>
<button id=b>Transpose</button></form>
<p class=note>The music is read from the page, so check the result. Bars the reader
was unsure of are printed in <span class=warn>red</span>; fix them in the MuseScore file if needed.</p>"""


def serve(port: int):
    from email.parser import BytesParser
    from email.policy import HTTP
    from html import escape
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    results: dict[str, tuple[str, Result]] = {}

    class H(BaseHTTPRequestHandler):
        def _send(self, body: bytes, ctype: str, extra=()):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.strip("/").split("/")
            if len(path) == 2 and path[0] in results and path[1] in ("pdf", "mscz"):
                name, r = results[path[0]]
                ctype = "application/pdf" if path[1] == "pdf" else "application/octet-stream"
                disp = "inline" if path[1] == "pdf" else "attachment"
                self._send(getattr(r, path[1]), ctype, [("Content-Disposition", f'{disp}; filename="{name}.{path[1]}"')])
            else:
                self._send((PAGE % "").encode(), "text/html")

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            msg = BytesParser(policy=HTTP).parsebytes(
                b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
            fields = {p.get_param("name", header="content-disposition"): p for p in msg.iter_parts()}
            f, n = fields["file"], int(fields["semitones"].get_content().strip() or 0)
            name = Path(f.get_filename() or "score.pdf")
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    src = Path(tmp) / f"in{name.suffix.lower()}"
                    src.write_bytes(f.get_payload(decode=True))
                    r = transpose(src, n)
            except Exception as e:
                self._send((PAGE % f"<p class=warn>Failed: {escape(str(e))}</p>").encode(), "text/html")
                return
            token = secrets.token_urlsafe(8)
            results[token] = (f"{name.stem}_{n:+d}", r)
            bars = (f"<p class=warn>Check bar{'s' if len(r.flagged) > 1 else ''} "
                    f"{', '.join(map(str, r.flagged))} (in red).</p>") if r.flagged else ""
            done = (f"<p><b>Done:</b> {escape(name.name)} {n:+d} semitones, {r.chords} chord symbols.</p>{bars}"
                    f"<p><a href=/{token}/pdf target=_blank>Open transposed PDF</a> &middot; "
                    f"<a href=/{token}/mscz>MuseScore file</a></p><hr>")
            self._send((PAGE % done).encode(), "text/html")

    print(f"http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", type=Path)
    ap.add_argument("semitones", nargs="?", type=int, default=0)
    ap.add_argument("-o", "--output", type=Path, help="output PDF (default: <input>_<+n>.pdf)")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--port", type=int, default=8771)
    a = ap.parse_args()
    if a.serve:
        serve(a.port)
    elif not a.input:
        ap.error("give an input file or --serve")
    else:
        out = a.output or a.input.with_name(f"{a.input.stem}_{a.semitones:+d}.pdf")
        r = transpose(a.input, a.semitones)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(r.pdf)
        out.with_suffix(".mscz").write_bytes(r.mscz)
        print(f"{out}  (+ {out.with_suffix('.mscz').name}, {r.chords} chord symbols)")
        if r.flagged:
            print(f"Check bars {', '.join(map(str, r.flagged))}: their rhythm didn't add up (printed in red).")
