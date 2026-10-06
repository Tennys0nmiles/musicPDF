#!/usr/bin/env bash
# Download the tools transposer drives (about 400 MB of downloads, 800 MB unpacked)
# into ./vendor and make a Python venv. Linux x86_64; tested on Ubuntu 24.04.
# Safe to re-run: finished steps are skipped.
set -euo pipefail
cd "$(dirname "$0")"

AUDIVERIS_URL=https://github.com/Audiveris/audiveris/releases/download/5.11.0/Audiveris-5.11.0-ubuntu24.04-x86_64.deb
MUSESCORE_URL=https://github.com/musescore/MuseScore/releases/download/v4.7.5/MuseScore-Studio-4.7.5.260831071-x86_64.AppImage
TESSDATA_URL=https://github.com/tesseract-ocr/tessdata/raw/main/eng.traineddata
TESSDATA_BEST_URL=https://github.com/tesseract-ocr/tessdata_best/raw/main/eng.traineddata

for cmd in python3 curl pdftoppm unzip; do
  command -v "$cmd" >/dev/null || { echo "missing '$cmd' (on Ubuntu: sudo apt install python3-venv curl poppler-utils unzip)"; exit 1; }
done

mkdir -p vendor/download

if [ ! -x vendor/audiveris/opt/audiveris/bin/Audiveris ]; then
  echo "== Audiveris (music recognition)"
  curl -fL --progress-bar -o vendor/download/audiveris.deb "$AUDIVERIS_URL"
  rm -rf vendor/audiveris && mkdir -p vendor/audiveris
  if command -v dpkg-deb >/dev/null; then
    dpkg-deb -x vendor/download/audiveris.deb vendor/audiveris
  else  # a .deb is an ar archive holding data.tar.*
    (cd vendor/download && ar x audiveris.deb) && tar -xf vendor/download/data.tar.* -C vendor/audiveris
  fi
  # Audiveris ships with an 8 GB Java heap; 2 GB is plenty for a song and leaves room on 16 GB machines.
  sed -i 's/^java-options=-Xmx.*/java-options=-Xmx2G/' vendor/audiveris/opt/audiveris/lib/app/Audiveris.cfg
fi

if [ ! -x vendor/musescore/AppRun ]; then
  echo "== MuseScore (transposing and engraving)"
  curl -fL --progress-bar -o vendor/download/musescore.AppImage "$MUSESCORE_URL"
  chmod +x vendor/download/musescore.AppImage
  rm -rf vendor/musescore
  (cd vendor/download && ./musescore.AppImage --appimage-extract >/dev/null && mv squashfs-root ../musescore)
fi

if [ ! -x vendor/tesseract/tesseract ]; then
  echo "== Tesseract (chord-name OCR), taken from Audiveris' bundled copy"
  app=vendor/audiveris/opt/audiveris/lib/app
  mkdir -p vendor/tesseract
  unzip -ojq "$app"/tesseract-*-linux-x86_64.jar 'org/bytedeco/tesseract/linux-x86_64/*' -d vendor/tesseract
  unzip -ojq "$app"/leptonica-*-linux-x86_64.jar 'org/bytedeco/leptonica/linux-x86_64/*.so*' -d vendor/tesseract
  rm -f vendor/tesseract/libjni*.so
  chmod +x vendor/tesseract/tesseract
fi

for dir in tessdata tessdata_best; do
  url=$TESSDATA_URL; [ "$dir" = tessdata_best ] && url=$TESSDATA_BEST_URL
  if [ ! -s "vendor/tesseract/$dir/eng.traineddata" ]; then
    echo "== Tesseract English model ($dir)"
    mkdir -p "vendor/tesseract/$dir"
    curl -fL --progress-bar -o "vendor/tesseract/$dir/eng.traineddata" "$url"
  fi
done

# Audiveris reads titles and lyrics with the same model, from its own config folder.
AUDIVERIS_TESSDATA="${XDG_CONFIG_HOME:-$HOME/.config}/AudiverisLtd/audiveris/tessdata"
if [ ! -s "$AUDIVERIS_TESSDATA/eng.traineddata" ]; then
  mkdir -p "$AUDIVERIS_TESSDATA"
  cp vendor/tesseract/tessdata/eng.traineddata "$AUDIVERIS_TESSDATA/"
fi

if [ ! -x .venv/bin/python ]; then
  echo "== Python venv"
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt

rm -rf vendor/download
echo "Done. Try: ./transpose.py your-score.pdf 2    or    ./transpose.py --serve"
