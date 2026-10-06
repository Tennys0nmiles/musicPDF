"""Page-image cleanup before OMR: erase guitar chord diagrams.

Fretboard grids (and their x/o markers and "3fr" labels) sit between the chord
names and the staff. Audiveris reads the "3" of "3fr" as a triplet and the grid
blocks chord-name linking, so they are removed; the chord names stay.
"""
import numpy as np
from scipy import ndimage


def _line_count(profile: np.ndarray, length: int, frac: float) -> int:
    """Number of separate runs of rows/columns inked over at least `frac` of `length`."""
    full = profile >= frac * length
    return int(np.count_nonzero(full[1:] & ~full[:-1]) + full[0])


def find_fretboards(ink: np.ndarray, dpi: int):
    """Bounding boxes (y0, y1, x0, x1) of chord-diagram grids in a binary page."""
    lab, _ = ndimage.label(ink, structure=np.ones((3, 3)))
    lo, hi = 0.08 * dpi, 0.75 * dpi  # grids are ~0.2-0.4 in. on typical scores
    boxes = []
    for i, sl in enumerate(ndimage.find_objects(lab), 1):
        h, w = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
        if not (lo <= h <= hi and lo <= w <= hi and 0.5 <= w / h <= 2.0):
            continue
        m = lab[sl] == i
        # Strings may be shortened by x/o markers fused on top; frets span the width.
        # Requiring 4+ full-width rows keeps beamed note groups out.
        if _line_count(m.sum(0), h, 0.6) >= 4 and _line_count(m.sum(1), w, 0.8) >= 4:
            boxes.append((sl[0].start, sl[0].stop, sl[1].start, sl[1].stop, i))
    return lab, boxes


def erase_fretboards(gray: np.ndarray, dpi: int) -> tuple[np.ndarray, int]:
    """Return a copy of a grayscale page with chord diagrams whited out."""
    ink = gray < 128
    lab, boxes = find_fretboards(ink, dpi)
    if not boxes:
        return gray, 0
    out = gray.copy()
    objs = ndimage.find_objects(lab)
    for y0, y1, x0, x1, gid in boxes:
        h, w = y1 - y0, x1 - x0
        ry0, ry1 = y0 - int(0.35 * h), y1 + int(0.05 * h)
        rx0, rx1 = x0 - int(0.3 * w), x1 + int(0.65 * w)
        # Erase the grid plus every small mark (dots, x/o, "3fr") wholly inside the margin.
        sub = lab[max(ry0, 0):ry1, max(rx0, 0):rx1]
        for j in np.unique(sub):
            if j == 0:
                continue
            s = objs[j - 1]
            if j == gid or (s[0].start >= ry0 and s[0].stop <= ry1 and s[1].start >= rx0 and s[1].stop <= rx1
                            and s[0].stop - s[0].start < 0.6 * h):
                region = lab[s] == j
                out[s][region] = 255
    return out, len(boxes)
