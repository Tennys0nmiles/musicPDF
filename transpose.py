#!/usr/bin/env python3
"""Transpose sheet-music PDFs by N semitones.

PDF -> erase guitar chord diagrams -> Audiveris (OMR) -> chord-symbol OCR ->
MusicXML repairs -> MuseScore (transpose + engrave) -> PDF (+ editable .mscz).

Bars the reader was unsure of (rhythm that doesn't add up, or notes Audiveris itself
had low confidence in) are printed in red and listed.

  ./transpose.py song.pdf 3          # up 3 semitones -> song_+3.pdf, song_+3.mscz
  ./transpose.py song.pdf -2 -o out.pdf
  ./transpose.py --serve             # web UI at http://localhost:8771
"""
import argparse, base64, json, os, secrets, shutil, subprocess, sys, tempfile, threading, time
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


# Share of the whole run each stage takes (measured on an 8-page scanned song).
STAGES = {"prepare": (0.00, 0.03), "omr": (0.03, 0.78), "chords": (0.78, 0.94), "engrave": (0.94, 1.00)}


def transpose(src: Path, semitones: int, progress=None, keep: Path | None = None) -> Result:
    """`progress(fraction, message)` reports how far along the whole run is (0..1).
    `keep`: folder to save intermediate files in (page images, Audiveris output,
    the repaired untransposed score.musicxml) for inspection or hand correction."""
    if not -12 <= semitones <= 12:
        raise ValueError("semitones must be between -12 and 12")

    def stage(name):
        lo, hi = STAGES[name]
        return lambda f, msg: progress and progress(lo + (hi - lo) * f, msg)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        if keep:
            keep.mkdir(parents=True, exist_ok=True)
            tmp = keep
        if src.suffix.lower() in (".mxl", ".musicxml", ".xml"):
            xml, flagged, n = src, [], 0
        else:
            stage("prepare")(0, "Preparing the pages")
            book = omr.run(src, tmp, stage("omr"))
            stage("chords")(0, "Reading chord names")
            found = chords.detect(book, stage("chords"))
            tree, flagged = fixup.fix(book, found)
            xml, n = tmp / "score.musicxml", len(found)
            tree.write(xml, encoding="UTF-8", xml_declaration=True)
        stage("engrave")(0, "Transposing and engraving (MuseScore)")
        pdf, mscz = musescore(xml, semitones, tmp)
    if progress:
        progress(1.0, "Done")
    return Result(pdf, mscz, flagged, n)


# ---------------------------------------------------------------- web UI

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>Transposer</title>
<style>
:root{--fg:#1d1d1f;--muted:#5f6368;--track:#e8eaed;--fill:#1a73e8;--warn:#b3261e;--bg:#fff}
@media (prefers-color-scheme:dark){:root{--fg:#e8eaed;--muted:#9aa0a6;--track:#3c4043;--fill:#8ab4f8;--warn:#f28b82;--bg:#202124}}
body{font:16px system-ui;max-width:32rem;margin:3rem auto;padding:0 1rem;line-height:1.5;color:var(--fg);background:var(--bg)}
input,button{font:inherit;margin:.4rem 0;display:block}button{padding:.5rem 1rem}
.note,#msg{color:var(--muted);font-size:.9rem}.warn{color:var(--warn)}
.bar{height:14px;background:var(--track);border-radius:7px;overflow:hidden;margin:1.2rem 0 .4rem}
#fill{height:100%;width:0;background:var(--fill);transition:width .6s ease}
#pct{font-variant-numeric:tabular-nums;font-weight:600}
</style>
<h2>Transpose sheet music</h2>
<form id=f>
<label>PDF (or MusicXML)<input type=file name=file accept=".pdf,.mxl,.musicxml,.xml" required></label>
<label>Semitones (+ up, &minus; down)<input type=number name=semitones value=0 min=-12 max=12></label>
<button id=b>Transpose</button></form>
<div id=prog hidden><div class=bar><div id=fill></div></div><span id=pct>0%</span> <span id=msg></span></div>
<div id=out></div>
<p class=note>The music is read from the page, so check the result. Bars the reader
was unsure of are printed in <span class=warn>red</span>; fix them in the MuseScore file if needed.</p>
<script>
const $ = id => document.getElementById(id);
const esc = s => s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function left(sec) { return sec < 60 ? 'under a minute left' : 'about ' + Math.round(sec / 60) + ' min left'; }
$('f').onsubmit = async e => {
  e.preventDefault();
  $('b').disabled = true; $('prog').hidden = false; $('out').innerHTML = '';
  $('fill').style.width = '0'; $('pct').textContent = '0%'; $('msg').textContent = 'Uploading';
  let job;
  try { job = (await (await fetch('/transpose', {method: 'POST', body: new FormData($('f'))})).json()).job; }
  catch (err) { $('out').innerHTML = '<p class=warn>Upload failed: ' + esc(String(err)) + '</p>'; $('b').disabled = false; return; }
  const t0 = Date.now();
  const poll = async () => {
    let s;
    try { s = await (await fetch('/status/' + job)).json(); } catch { return setTimeout(poll, 2000); }
    const pct = Math.floor(s.pct * 100), sec = (Date.now() - t0) / 1000;
    $('fill').style.width = (s.pct * 100).toFixed(1) + '%';
    $('pct').textContent = pct + '%';
    $('msg').textContent = s.msg + (s.pct > 0.08 && !s.done ? ' \u00b7 ' + left(sec * (1 - s.pct) / s.pct) : '');
    if (s.error) {
      $('out').innerHTML = '<p class=warn>Failed: ' + esc(s.error) + '</p>'; $('b').disabled = false;
    } else if (s.done) {
      const bars = s.flagged.length ? '<p class=warn>Check bar' + (s.flagged.length > 1 ? 's ' : ' ') + s.flagged.join(', ') + ' (in red).</p>' : '';
      $('out').innerHTML = '<p><b>Done</b> in ' + Math.round(sec) + ' s: ' + esc(s.name) + ', ' + s.chords + ' chord symbols.</p>' + bars +
        '<p><a href="/' + job + '/pdf" target=_blank>Open transposed PDF</a> &middot; <a href="/' + job + '/mscz">MuseScore file</a></p>';
      $('b').disabled = false;
    } else setTimeout(poll, 1000);
  };
  poll();
};
</script>"""


def serve(port: int):
    from email.parser import BytesParser
    from email.policy import HTTP
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    jobs: dict[str, dict] = {}
    one_at_a_time = threading.Lock()  # a run is a heavy job; never stack two

    def run(job: dict, src: Path, n: int):
        def progress(f, msg):
            job.update(pct=f, msg=msg)
        try:
            job["msg"] = "Waiting for the previous run to finish"
            with one_at_a_time:
                job["result"] = transpose(src, n, progress)
        except Exception as e:  # shown on the page
            job["error"] = str(e) or type(e).__name__
        finally:
            shutil.rmtree(src.parent, ignore_errors=True)
            job["done"] = True

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
            if len(path) == 2 and path[0] == "status" and path[1] in jobs:
                j = jobs[path[1]]
                r = j.get("result")
                status = {k: j[k] for k in ("pct", "msg", "name")} | {
                    "done": j["done"] and r is not None, "error": j.get("error"),
                    "flagged": r.flagged if r else [], "chords": r.chords if r else 0}
                self._send(json.dumps(status).encode(), "application/json")
            elif len(path) == 2 and path[0] in jobs and path[1] in ("pdf", "mscz") and jobs[path[0]].get("result"):
                j = jobs[path[0]]
                ctype = "application/pdf" if path[1] == "pdf" else "application/octet-stream"
                disp = "inline" if path[1] == "pdf" else "attachment"
                self._send(getattr(j["result"], path[1]), ctype,
                           [("Content-Disposition", f'{disp}; filename="{j["name"]}.{path[1]}"')])
            else:
                self._send(PAGE.encode(), "text/html")

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            msg = BytesParser(policy=HTTP).parsebytes(
                b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
            fields = {p.get_param("name", header="content-disposition"): p for p in msg.iter_parts()}
            f, n = fields["file"], int(fields["semitones"].get_content().strip() or 0)
            name = Path(f.get_filename() or "score.pdf")
            src = Path(tempfile.mkdtemp(prefix="transposer-")) / f"in{name.suffix.lower()}"
            src.write_bytes(f.get_payload(decode=True))
            token = secrets.token_urlsafe(8)
            jobs[token] = {"pct": 0.0, "msg": "Starting", "name": f"{name.stem}_{n:+d}", "done": False}
            threading.Thread(target=run, args=(jobs[token], src, n), daemon=True).start()
            self._send(json.dumps({"job": token}).encode(), "application/json")

        def log_message(self, fmt, *args):
            if not self.path.startswith("/status/"):  # the page polls every second
                super().log_message(fmt, *args)

    print(f"http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


def text_bar(f: float, msg: str):
    """One self-updating progress line for the terminal."""
    width = 30
    filled = int(f * width)
    sys.stderr.write(f"\r[{'#' * filled}{'-' * (width - filled)}] {f * 100:3.0f}%  {msg[:60]:<60}")
    if f >= 1:
        sys.stderr.write("\n")
    sys.stderr.flush()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", type=Path)
    ap.add_argument("semitones", nargs="?", type=int, default=0)
    ap.add_argument("-o", "--output", type=Path, help="output PDF (default: <input>_<+n>.pdf)")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--port", type=int, default=8771)
    ap.add_argument("--keep", type=Path, metavar="DIR", help="keep intermediate files (incl. score.musicxml) in DIR")
    a = ap.parse_args()
    if a.serve:
        serve(a.port)
    elif not a.input:
        ap.error("give an input file or --serve")
    else:
        out = a.output or a.input.with_name(f"{a.input.stem}_{a.semitones:+d}.pdf")
        r = transpose(a.input, a.semitones, text_bar, a.keep)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(r.pdf)
        out.with_suffix(".mscz").write_bytes(r.mscz)
        print(f"{out}  (+ {out.with_suffix('.mscz').name}, {r.chords} chord symbols)")
        if r.flagged:
            print(f"Check bars {', '.join(map(str, r.flagged))} (printed in red): the reader was unsure of them.")
