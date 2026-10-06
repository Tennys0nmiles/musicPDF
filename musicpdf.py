#!/usr/bin/env python3
"""musicPDF: read a sheet-music PDF, then transpose it and/or make practice tracks.

PDF -> erase guitar chord diagrams -> Audiveris (OMR) -> chord-symbol OCR ->
MusicXML repairs -> MuseScore: transposed PDF (+ editable .mscz) and/or plain-piano
practice tracks (MP3) of the whole piece or just its main line.

Bars the reader was unsure of (rhythm that doesn't add up, or notes Audiveris itself
had low confidence in) are printed in red and listed.

  ./musicpdf.py song.pdf --key Am --direction down    # to C major / A minor -> song_-7.pdf
  ./musicpdf.py song.pdf --semitones 3                # up 3 semitones -> song_+3.pdf
  ./musicpdf.py song.pdf --track both --speed 80      # practice tracks in the original key
  ./musicpdf.py song.pdf --key Am --track main        # both: transposed PDF + its track
  ./musicpdf.py --serve                               # web page at http://localhost:8771
"""
import argparse, json, os, secrets, shutil, sys, tempfile, threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV_PY = HERE / ".venv/bin/python"
if VENV_PY.exists() and Path(sys.prefix).resolve() != (HERE / ".venv").resolve():
    os.execv(VENV_PY, [str(VENV_PY), *sys.argv])  # run inside the project venv made by setup.sh
sys.path.insert(0, str(HERE))
import audio, chords, fixup, omr  # noqa: E402
from engrave import KEYS, first_key, key_name, musescore, parse_key, semitones_between, target_key  # noqa: E402


@dataclass
class Options:
    semitones: int | None = None   # transpose by this many semitones...
    key: int | None = None         # ...or into this key signature (sharps +, flats -)
    direction: str = "closest"     # with key: closest, up or down
    tracks: tuple[str, ...] = ()   # practice tracks: "full" and/or "main"
    speed: float = 100             # practice track tempo, percent

    @property
    def transpose(self) -> bool:
        return self.semitones is not None or self.key is not None


@dataclass
class Result:
    pdf: bytes                     # the score: transposed if asked, else as read
    mscz: bytes
    flagged: list[int] = field(default_factory=list)  # bar numbers to check by eye
    chords: int = 0
    xml: bytes = b""               # the repaired score before transposing (for more tracks)
    semitones: int = 0
    from_key: int = 0
    to_key: int = 0
    tracks: dict[str, bytes] = field(default_factory=dict)  # "full"/"main" -> MP3


def process(src: Path, opts: Options, progress=None, keep: Path | None = None) -> Result:
    """Read `src` (PDF, or MusicXML to skip reading) and make what `opts` asks for.
    `progress(fraction, message)` reports how far along the whole run is (0..1).
    `keep`: folder to save intermediate files in (page images, Audiveris output,
    the repaired untransposed score.musicxml) for inspection or hand correction."""
    if opts.semitones is not None and not -12 <= opts.semitones <= 12:
        raise ValueError("semitones must be between -12 and 12")
    # Share of the whole run each stage takes (measured on an 8-page scanned song).
    stages = {"prepare": (0.00, 0.03), "omr": (0.03, 0.74), "chords": (0.74, 0.89)}
    stages["engrave"] = (0.89, 0.93) if opts.tracks else (0.89, 1.00)
    stages["tracks"] = (0.93, 1.00)

    def stage(name):
        lo, hi = stages[name]
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
        if xml.suffix.lower() == ".mxl":
            xml_bytes = ET.tostring(fixup.load_xml(xml).getroot(), encoding="UTF-8", xml_declaration=True)
            xml = tmp / "score.musicxml"
            xml.write_bytes(xml_bytes)
        src_key = first_key(xml)
        to_key = opts.key if opts.key is not None else None
        if opts.key is not None:
            semitones = semitones_between(src_key, opts.key, opts.direction)
        else:
            semitones = opts.semitones or 0
        stage("engrave")(0, "Transposing and engraving (MuseScore)" if opts.transpose else "Engraving (MuseScore)")
        pdf, mscz = musescore(xml, semitones, tmp, to_key)
        result = Result(pdf, mscz, flagged, n, xml.read_bytes(), semitones, src_key,
                        to_key if to_key is not None else target_key(src_key, semitones))
        for i, mode in enumerate(opts.tracks):
            stage("tracks")(i / len(opts.tracks), f"Making the {'whole-piece' if mode == 'full' else 'main-line'} track")
            result.tracks[mode] = audio.render(result.xml, semitones, mode, "mp3", opts.speed, to_key)
    if progress:
        progress(1.0, "Done")
    return result


# ---------------------------------------------------------------- web UI

KEY_OPTIONS = "".join(f'<option value="{f}"{" selected" if f == 0 else ""}>{key_name(f)}</option>' for f in KEYS)

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>musicPDF</title>
<style>
:root{--fg:#1d1d1f;--muted:#5f6368;--track:#e8eaed;--fill:#1a73e8;--warn:#b3261e;--bg:#fff;--box:#f6f8fa}
@media (prefers-color-scheme:dark){:root{--fg:#e8eaed;--muted:#9aa0a6;--track:#3c4043;--fill:#8ab4f8;--warn:#f28b82;--bg:#202124;--box:#292a2d}}
body{font:16px system-ui;max-width:34rem;margin:3rem auto;padding:0 1rem;line-height:1.5;color:var(--fg);background:var(--bg)}
input,button,select{font:inherit}button{padding:.5rem 1.2rem;margin-top:.8rem}
fieldset{border:1px solid var(--track);border-radius:8px;margin:.8rem 0;padding:.4rem .9rem .7rem;background:var(--box)}
legend{font-weight:600;padding:0 .3rem}
.opt{margin:.35rem 0 .35rem 1.6rem}.opt label{margin-right:.8rem}
.off{opacity:.45;pointer-events:none}
input[type=number]{width:4.5rem}
.note,#msg{color:var(--muted);font-size:.9rem}.warn{color:var(--warn)}
.bar{height:14px;background:var(--track);border-radius:7px;overflow:hidden;margin:1.2rem 0 .4rem}
#fill{height:100%;width:0;background:var(--fill);transition:width .6s ease}
#pct{font-variant-numeric:tabular-nums;font-weight:600}
.tracks{border-top:1px solid var(--track);margin-top:1rem;padding-top:.6rem}
.row{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}.row button{margin:.2rem 0}
audio{width:100%;margin:.3rem 0}
</style>
<h2>musicPDF</h2>
<p class=note>Put in a sheet-music PDF; get it transposed, practice tracks of it, or both.</p>
<form id=f>
<label>Sheet music (PDF, or MusicXML)<br><input type=file name=file accept=".pdf,.mxl,.musicxml,.xml" required></label>
<fieldset><legend><label><input type=checkbox name=transpose id=tr checked> Transpose</label></legend>
 <div id=trbox>
 <div><label><input type=radio name=how value=key checked> To key</label></div>
 <div class=opt id=bykey><select name=key>__KEYS__</select>
  <select name=direction><option value=closest>closest</option><option value=up>up</option><option value=down>down</option></select></div>
 <div><label><input type=radio name=how value=semitones> By semitones</label></div>
 <div class="opt off" id=bysemi><input type=number name=semitones value=0 min=-12 max=12> <span class=note>(+ up, &minus; down)</span></div>
 </div></fieldset>
<fieldset><legend><label><input type=checkbox name=tracks id=tk> Practice tracks</label> <span class=note>(plain piano)</span></legend>
 <div id=tkbox class=off>
 <div class=opt><label><input type=checkbox name=full checked> Whole piece</label>
  <label><input type=checkbox name=main checked> Main line only</label></div>
 <div class=opt>Speed <input type=number name=speed value=100 min=10 max=300 step=5>%%</div>
 </div></fieldset>
<button id=b>Go</button></form>
<div id=prog hidden><div class=bar><div id=fill></div></div><span id=pct>0%%</span> <span id=msg></span></div>
<div id=out></div>
<p class=note>The music is read from the page, so check the result. Bars the reader
was unsure of are printed in <span class=warn>red</span>; fix them in the MuseScore file if needed.</p>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function left(sec) { return sec < 60 ? 'under a minute left' : 'about ' + Math.round(sec / 60) + ' min left'; }
function sync() {
  $('trbox').classList.toggle('off', !$('tr').checked);
  $('tkbox').classList.toggle('off', !$('tk').checked);
  const byKey = document.querySelector('input[name=how]:checked').value === 'key';
  $('bykey').classList.toggle('off', !byKey); $('bysemi').classList.toggle('off', byKey);
}
document.querySelectorAll('#f input').forEach(i => i.addEventListener('change', sync)); sync();
const label = (mode, speed) => (mode === 'full' ? 'Whole piece' : 'Main line') + (speed != 100 ? ' at ' + speed + '%%' : '');
function player(job, key) {
  const [mode, speed] = key.split('-'), url = '/' + job + '/audio/' + key + '.mp3';
  return '<p>' + label(mode, speed) + ' &middot; <a href="' + url + '?download=1">download MP3</a></p><audio controls preload=none src="' + url + '"></audio>';
}
$('f').onsubmit = async e => {
  e.preventDefault();
  const fd = new FormData($('f'));
  if (!$('tr').checked && !$('tk').checked) { $('out').innerHTML = '<p class=warn>Choose Transpose, Practice tracks, or both.</p>'; return; }
  if ($('tk').checked && !fd.get('full') && !fd.get('main')) { $('out').innerHTML = '<p class=warn>Pick at least one practice track.</p>'; return; }
  $('b').disabled = true; $('prog').hidden = false; $('out').innerHTML = '';
  $('fill').style.width = '0'; $('pct').textContent = '0%%'; $('msg').textContent = 'Uploading';
  let job;
  try { job = (await (await fetch('/run', {method: 'POST', body: fd})).json()).job; }
  catch (err) { $('out').innerHTML = '<p class=warn>Upload failed: ' + esc(err) + '</p>'; $('b').disabled = false; return; }
  const t0 = Date.now();
  const poll = async () => {
    let s;
    try { s = await (await fetch('/status/' + job)).json(); } catch { return setTimeout(poll, 2000); }
    const sec = (Date.now() - t0) / 1000;
    $('fill').style.width = (s.pct * 100).toFixed(1) + '%%';
    $('pct').textContent = Math.floor(s.pct * 100) + '%%';
    $('msg').textContent = s.msg + (s.pct > 0.08 && !s.done ? ' \\u00b7 ' + left(sec * (1 - s.pct) / s.pct) : '');
    if (s.error) { $('out').innerHTML = '<p class=warn>Failed: ' + esc(s.error) + '</p>'; $('b').disabled = false; return; }
    if (!s.done) return setTimeout(poll, 1000);
    let html = '<p><b>Done</b> in ' + Math.round(sec) + ' s' + (s.chords ? ', ' + s.chords + ' chord symbols' : '') + '.</p>';
    if (s.flagged.length) html += '<p class=warn>Check bar' + (s.flagged.length > 1 ? 's ' : ' ') + s.flagged.join(', ') + ' (in red).</p>';
    if (s.transposed) {
      const n = s.semitones;
      html += '<p>' + esc(s.from_key) + ' &rarr; <b>' + esc(s.to_key) + '</b> (' + (n > 0 ? '+' : n < 0 ? '&minus;' : '') + Math.abs(n) + ' semitone' + (Math.abs(n) === 1 ? '' : 's') + ')</p>' +
        '<p><a href="/' + job + '/pdf" target=_blank>Open transposed PDF</a> &middot; <a href="/' + job + '/mscz">MuseScore file</a></p>';
    } else {
      html += '<p class=note>Key: ' + esc(s.from_key) + ' (not transposed) &middot; <a href="/' + job + '/pdf" target=_blank>score as read</a> &middot; <a href="/' + job + '/mscz">MuseScore file</a></p>';
    }
    html += '<div class=tracks><b>Practice tracks</b> <span class=note>(plain piano' + (s.transposed ? ', in the new key' : '') + ')</span><div id=players>' +
      Object.keys(s.audio).filter(k => s.audio[k].state === 'ready').map(k => '<div id="p-' + k + '">' + player(job, k) + '</div>').join('') +
      '</div><div class=row>Make another: <label>speed <input type=number id=speed value=100 min=10 max=300 step=5>%%</label>' +
      '<button data-mode=full>Whole piece</button><button data-mode=main>Main line</button></div></div>';
    $('out').innerHTML = html;
    document.querySelectorAll('[data-mode]').forEach(btn => btn.onclick = () => track(job, btn.dataset.mode));
    $('b').disabled = false;
  };
  poll();
};
async function track(job, mode) {
  const speed = Math.round(Number($('speed').value) || 100), key = mode + '-' + speed, id = 'p-' + key;
  if (!$(id)) $('players').insertAdjacentHTML('beforeend', '<div id="' + id + '"></div>');
  $(id).innerHTML = '<p class=note>' + label(mode, speed) + ': rendering...</p>';
  const body = new FormData(); body.append('mode', mode); body.append('speed', speed);
  await fetch('/audio/' + job, {method: 'POST', body});
  const poll = async () => {
    const a = ((await (await fetch('/status/' + job)).json()).audio || {})[key];
    if (!a || a.state === 'working' || a.state === 'queued') return setTimeout(poll, 1000);
    $(id).innerHTML = a.state === 'error' ? '<p class=warn>' + label(mode, speed) + ' failed: ' + esc(a.error) + '</p>' : player(job, key);
  };
  poll();
}
</script>""".replace("%%", "%").replace("__KEYS__", KEY_OPTIONS)


def serve(port: int):
    from email.parser import BytesParser
    from email.policy import HTTP
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    jobs: dict[str, dict] = {}
    one_at_a_time = threading.Lock()  # a run is a heavy job; never stack two

    def run_audio(job: dict, key: str, mode: str, speed: float):
        job["audio"][key] = {"state": "queued"}
        try:
            with one_at_a_time:
                job["audio"][key] = {"state": "working"}
                r = job["result"]
                data = audio.render(r.xml, r.semitones, mode, "mp3", speed, r.to_key if job["opts"].transpose else None)
            job["audio"][key] = {"state": "ready", "data": data}
        except Exception as e:
            job["audio"][key] = {"state": "error", "error": str(e) or type(e).__name__}

    def run(job: dict, src: Path, opts: Options):
        def progress(f, msg):
            job.update(pct=f, msg=msg)
        try:
            job["msg"] = "Waiting for the previous run to finish"
            with one_at_a_time:
                r = process(src, opts, progress)
            for mode, data in r.tracks.items():
                job["audio"][f"{mode}-{opts.speed:g}"] = {"state": "ready", "data": data}
            job["result"] = r
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
            path = self.path.split("?")[0].strip("/").split("/")
            query = self.path.partition("?")[2]
            if len(path) == 2 and path[0] == "status" and path[1] in jobs:
                j = jobs[path[1]]
                r = j.get("result")
                status = {k: j[k] for k in ("pct", "msg", "name")} | {
                    "done": j["done"] and r is not None, "error": j.get("error"),
                    "flagged": r.flagged if r else [], "chords": r.chords if r else 0,
                    "transposed": j["opts"].transpose, "semitones": r.semitones if r else 0,
                    "from_key": key_name(r.from_key) if r else "", "to_key": key_name(r.to_key) if r else "",
                    "audio": {k: {"state": a["state"], "error": a.get("error")} for k, a in j["audio"].items()}}
                self._send(json.dumps(status).encode(), "application/json")
            elif (len(path) == 3 and path[0] in jobs and path[1] == "audio"
                  and jobs[path[0]]["audio"].get(path[2].removesuffix(".mp3"), {}).get("state") == "ready"):
                key = path[2].removesuffix(".mp3")
                disp = "attachment" if "download=1" in query else "inline"
                self._send(jobs[path[0]]["audio"][key]["data"], "audio/mpeg",
                           [("Content-Disposition", f'{disp}; filename="{jobs[path[0]]["name"]}_{key}.mp3"')])
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
            parts = {p.get_param("name", header="content-disposition"): p for p in msg.iter_parts()}
            field_ = lambda k, d="": parts[k].get_content().strip() if k in parts else d
            path = self.path.strip("/").split("/")
            if len(path) == 2 and path[0] == "audio" and path[1] in jobs and jobs[path[1]].get("result"):
                mode, speed = field_("mode"), float(field_("speed", "100") or 100)
                key = f"{mode}-{speed:g}"
                if jobs[path[1]]["audio"].get(key, {}).get("state") not in ("queued", "working", "ready"):
                    threading.Thread(target=run_audio, args=(jobs[path[1]], key, mode, speed), daemon=True).start()
                self._send(json.dumps({"key": key}).encode(), "application/json")
                return
            opts = Options()
            if field_("transpose"):
                if field_("how", "key") == "key":
                    opts.key, opts.direction = int(field_("key", "0")), field_("direction", "closest")
                else:
                    opts.semitones = int(field_("semitones", "0") or 0)
            if field_("tracks"):
                opts.tracks = tuple(m for m in ("full", "main") if field_(m))
                opts.speed = float(field_("speed", "100") or 100)
            f = parts["file"]
            name = Path(f.get_filename() or "score.pdf")
            src = Path(tempfile.mkdtemp(prefix="musicpdf-")) / f"in{name.suffix.lower()}"
            src.write_bytes(f.get_payload(decode=True))
            token = secrets.token_urlsafe(8)
            jobs[token] = {"pct": 0.0, "msg": "Starting", "name": name.stem, "done": False, "audio": {}, "opts": opts}
            threading.Thread(target=run, args=(jobs[token], src, opts), daemon=True).start()
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
    ap.add_argument("input", nargs="?", type=Path, help="sheet-music PDF (or MusicXML)")
    how = ap.add_mutually_exclusive_group()
    how.add_argument("--key", help="transpose to this key: Am, Bb, F# major, ... (relative keys share a "
                                   "signature, so Am and C give the same result)")
    how.add_argument("--semitones", type=int, help="transpose by this many semitones (-12..12)")
    ap.add_argument("--direction", choices=("closest", "up", "down"), default="closest",
                    help="with --key: move up, down, or whichever is closer (default)")
    ap.add_argument("--track", choices=("full", "main", "both"),
                    help="practice tracks (plain piano MP3): the whole piece, the main line, or both")
    ap.add_argument("--speed", type=float, default=100, metavar="PCT", help="practice track speed in percent (default 100)")
    ap.add_argument("-o", "--output", type=Path, metavar="PDF", help="output name (default: next to the input)")
    ap.add_argument("--keep", type=Path, metavar="DIR", help="keep intermediate files (incl. score.musicxml) in DIR")
    ap.add_argument("--serve", action="store_true", help="run the web page instead")
    ap.add_argument("--port", type=int, default=8771)
    a = ap.parse_args()
    if a.serve:
        serve(a.port)
        sys.exit()
    if not a.input:
        ap.error("give a sheet-music PDF (or --serve)")
    opts = Options(semitones=a.semitones, key=parse_key(a.key) if a.key else None, direction=a.direction,
                   tracks={"both": ("full", "main"), None: ()}.get(a.track, (a.track,)), speed=a.speed)
    if not opts.transpose and not opts.tracks:
        ap.error("choose what to make: --key or --semitones (transpose) and/or --track (practice tracks)")
    r = process(a.input, opts, text_bar, a.keep)
    if opts.transpose:
        print(f"{key_name(r.from_key)} -> {key_name(r.to_key)} ({r.semitones:+d} semitones)")
    base = a.output or a.input.with_name(f"{a.input.stem}_{r.semitones:+d}.pdf" if opts.transpose
                                         else f"{a.input.stem}.pdf")
    base.parent.mkdir(parents=True, exist_ok=True)
    if opts.transpose:
        base.write_bytes(r.pdf)
        base.with_suffix(".mscz").write_bytes(r.mscz)
        print(f"{base}  (+ {base.with_suffix('.mscz').name}" + (f", {r.chords} chord symbols)" if r.chords else ")"))
    for mode, data in r.tracks.items():
        track = base.with_name(f"{base.stem}_{mode}{'' if a.speed == 100 else f'_{a.speed:g}pct'}.mp3")
        track.write_bytes(data)
        print(track)
    if r.flagged:
        print(f"Check bars {', '.join(map(str, r.flagged))} (printed in red): the reader was unsure of them.")
