#!/usr/bin/env python3
"""
make_demo_gif.py -- record the dashboard as an animated GIF.

Frames come from headless Chromium (Edge or Chrome, whichever is installed --
nothing is downloaded). Assembly is pure standard library: a PNG reader built on
zlib, a median-cut quantiser and a GIF89a/LZW writer. No Pillow, no ffmpeg, no
node_modules, in keeping with the rest of the project.

Usage:
    tools/make_demo_gif.py [--out docs/dashboard.gif] [--width 1200] [--scale 0.5]
                           [--delay 140] [--page <index.html>]
"""

from __future__ import annotations

import argparse
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (tab, theme, command-picker index or None) -- the tour the GIF walks through.
# Indices follow dashboard.command_menu(): 2 = serve, 5 = doctor, 9 = map,
# 11 = dry run, 12 = post for real (the one styled as dangerous).
FRAMES = [
    ("projects", "light", None),
    ("sessions", "light", None),
    ("queue",    "light", None),
    ("posted",   "light", None),
    ("unmapped", "light", None),
    ("health",   "light", None),
    ("projects", "light", 2),
    ("projects", "light", 5),
    ("projects", "light", 11),
    ("projects", "light", 12),
    ("projects", "light", 9),
    ("health",   "dark",  None),
    ("projects", "dark",  None),
]

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find_browser() -> str:
    for candidate in BROWSERS:
        if Path(candidate).exists():
            return candidate
    for name in ("chromium", "google-chrome", "chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    raise SystemExit("no Chromium-based browser found; install Edge or Chrome")


# ---------------------------------------------------------------- PNG reading

def read_png(path: Path) -> tuple[int, int, bytearray]:
    """Minimal PNG reader: 8-bit RGB or RGBA, non-interlaced. Returns RGB."""
    raw = path.read_bytes()
    if raw[:8] != bytes([137, 80, 78, 71, 13, 10, 26, 10]):
        raise ValueError(f"{path} is not a PNG")
    pos, idat, width, height, channels = 8, bytearray(), 0, 0, 0
    while pos < len(raw):
        length = struct.unpack(">I", raw[pos:pos + 4])[0]
        kind = raw[pos + 4:pos + 8]
        body = raw[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, colour = struct.unpack(">IIBB", body[:10])
            if depth != 8 or colour not in (2, 6):
                raise ValueError(f"unsupported PNG: depth={depth} colour={colour}")
            channels = 3 if colour == 2 else 4
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
        pos += 12 + length

    data = zlib.decompress(bytes(idat))
    stride = width * channels
    out = bytearray(width * height * 3)
    prev = bytearray(stride)
    at = 0
    for y in range(height):
        filt = data[at]; at += 1
        line = bytearray(data[at:at + stride]); at += stride
        if filt == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif filt == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif filt == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif filt == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        elif filt != 0:
            raise ValueError(f"bad PNG filter {filt}")
        prev = line
        base = y * width * 3
        for x in range(width):
            src = x * channels
            out[base + x * 3] = line[src]
            out[base + x * 3 + 1] = line[src + 1]
            out[base + x * 3 + 2] = line[src + 2]
    return width, height, out


def downscale(width: int, height: int, rgb: bytearray, factor: float):
    """Box-average downscale. Halving keeps text legible while cutting the file
    to a quarter; GIFs of a dashboard are mostly flat colour, so this is cheap."""
    if factor >= 0.999:
        return width, height, rgb
    nw, nh = max(1, int(width * factor)), max(1, int(height * factor))
    out = bytearray(nw * nh * 3)
    xs = [(int(x * width / nw), max(int((x + 1) * width / nw), int(x * width / nw) + 1))
          for x in range(nw)]
    ys = [(int(y * height / nh), max(int((y + 1) * height / nh), int(y * height / nh) + 1))
          for y in range(nh)]
    for oy in range(nh):
        y0, y1 = ys[oy]
        for ox in range(nw):
            x0, x1 = xs[ox]
            r = g = b = n = 0
            for y in range(y0, y1):
                row = y * width * 3
                for x in range(x0, x1):
                    i = row + x * 3
                    r += rgb[i]; g += rgb[i + 1]; b += rgb[i + 2]; n += 1
            o = (oy * nw + ox) * 3
            out[o] = r // n; out[o + 1] = g // n; out[o + 2] = b // n
    return nw, nh, out


# ---------------------------------------------------------------- quantising

def median_cut(pixels: list[tuple[int, int, int]], depth: int = 8) -> list[tuple[int, int, int]]:
    """Classic median cut to at most 2**depth colours."""
    boxes = [pixels]
    for _ in range(depth):
        nxt = []
        for box in boxes:
            if len(box) <= 1:
                nxt.append(box)
                continue
            ranges = [max(p[c] for p in box) - min(p[c] for p in box) for c in range(3)]
            axis = ranges.index(max(ranges))
            box.sort(key=lambda p: p[axis])
            mid = len(box) // 2
            nxt.extend([box[:mid], box[mid:]])
        boxes = [b for b in nxt if b]
        if len(boxes) >= 2 ** depth:
            break
    palette = []
    for box in boxes[:256]:
        n = len(box)
        palette.append((sum(p[0] for p in box) // n,
                        sum(p[1] for p in box) // n,
                        sum(p[2] for p in box) // n))
    while len(palette) < 2:
        palette.append((0, 0, 0))
    return palette


def build_palette(frames: list[tuple[int, int, bytearray]]) -> list[tuple[int, int, int]]:
    """One global palette across every frame, so colours cannot shift mid-GIF."""
    seen: dict[tuple[int, int, int], int] = {}
    for _, _, rgb in frames:
        for i in range(0, len(rgb), 3):
            key = (rgb[i], rgb[i + 1], rgb[i + 2])
            seen[key] = seen.get(key, 0) + 1
    if len(seen) <= 256:
        return list(seen.keys())
    # sample proportionally to frequency so flat backgrounds do not swamp text
    sample = []
    for colour, count in seen.items():
        sample.extend([colour] * min(count, 40))
    return median_cut(sample, 8)


def index_frame(rgb: bytearray, palette: list[tuple[int, int, int]],
                cache: dict) -> bytearray:
    out = bytearray(len(rgb) // 3)
    for i in range(0, len(rgb), 3):
        key = (rgb[i], rgb[i + 1], rgb[i + 2])
        best = cache.get(key)
        if best is None:
            br, bg, bb = key
            best, bestd = 0, 1 << 30
            for idx, (r, g, b) in enumerate(palette):
                d = (r - br) ** 2 + (g - bg) ** 2 + (b - bb) ** 2
                if d < bestd:
                    best, bestd = idx, d
                    if d == 0:
                        break
            cache[key] = best
        out[i // 3] = best
    return out


# ---------------------------------------------------------------- GIF writing

def lzw_encode(indices: bytearray, min_code_size: int) -> bytes:
    clear, end = 1 << min_code_size, (1 << min_code_size) + 1
    table = {(i,): i for i in range(1 << min_code_size)}
    nxt, size = end + 1, min_code_size + 1
    bits, nbits, out = 0, 0, bytearray()

    def emit(code):
        nonlocal bits, nbits
        bits |= code << nbits
        nbits += size
        while nbits >= 8:
            out.append(bits & 0xFF)
            bits >>= 8
            nbits -= 8

    emit(clear)
    prefix = ()
    for value in indices:
        candidate = prefix + (value,)
        if candidate in table:
            prefix = candidate
            continue
        emit(table[prefix])
        table[candidate] = nxt
        nxt += 1
        if nxt > (1 << size) and size < 12:
            size += 1
        elif nxt >= 4096:
            emit(clear)
            table = {(i,): i for i in range(1 << min_code_size)}
            nxt, size = end + 1, min_code_size + 1
        prefix = (value,)
    if prefix:
        emit(table[prefix])
    emit(end)
    if nbits:
        out.append(bits & 0xFF)
    return bytes(out)


def write_gif(path: Path, width: int, height: int,
              frames: list[bytearray], palette: list[tuple[int, int, int]],
              delay_cs: int) -> None:
    bits = max(1, (len(palette) - 1).bit_length())
    size = 1 << bits
    table = bytearray()
    for i in range(size):
        r, g, b = palette[i] if i < len(palette) else (0, 0, 0)
        table += bytes((r, g, b))

    out = bytearray(b"GIF89a")
    out += struct.pack("<HH", width, height)
    out += bytes((0xF0 | (bits - 1), 0, 0))
    out += table
    out += b"\x21\xFF\x0BNETSCAPE2.0\x03\x01\x00\x00\x00"      # loop forever

    min_code_size = max(2, bits)
    for indices in frames:
        out += b"\x21\xF9\x04\x04" + struct.pack("<H", delay_cs) + b"\x00\x00"
        out += b"\x2C" + struct.pack("<HHHH", 0, 0, width, height) + b"\x00"
        out += bytes((min_code_size,))
        data = lzw_encode(indices, min_code_size)
        for i in range(0, len(data), 255):
            chunk = data[i:i + 255]
            out += bytes((len(chunk),)) + chunk
        out += b"\x00"
    out += b"\x3B"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))


# ---------------------------------------------------------------- capture

INJECT = """
<script>
document.addEventListener('DOMContentLoaded', function(){
  setTimeout(function(){
    document.documentElement.setAttribute('data-theme', '__THEME__');
    var b = Array.prototype.slice.call(document.querySelectorAll('nav.tabs button'))
      .filter(function(x){ return x.dataset.tab === '__TAB__'; })[0];
    if (b) b.click();
    var a = document.getElementById('auto'); if (a) a.setAttribute('aria-pressed','false');
    var ci = __CMD__;
    if (ci !== null) {
      // Set the picker's state directly rather than dispatching 'change'. A
      // change fires a clipboard write, which cannot succeed headlessly and
      // leaves the button reading "Selected -- press Ctrl-C", wrapping it onto
      // a second line and shifting the layout mid-animation.
      var sel = document.getElementById('cmd');
      var out = document.getElementById('cmdout');
      var btn = document.getElementById('cmdcopy');
      var cmd = (window.__WORKLOG__.commands || [])[ci];
      if (sel && out && cmd) {
        sel.value = String(ci);
        out.textContent = cmd.cmd;
        out.classList.toggle('danger', !!cmd.danger);
        if (btn) btn.textContent = cmd.danger ? 'Copy (careful)' : 'Copy';
      }
    }
  }, 30);
});
</script>
"""


def capture(page: Path, width: int, out_dir: Path, browser: str) -> list[Path]:
    html = page.read_text(encoding="utf-8")
    shots = []
    for n, (tab, theme, cmd) in enumerate(FRAMES):
        variant = out_dir / f"v{n}.html"
        variant.write_text(
            html.replace("</body>", INJECT.replace("__TAB__", tab)
                                          .replace("__THEME__", theme)
                                          .replace("__CMD__", "null" if cmd is None else str(cmd))
                         + "</body>"),
            encoding="utf-8")
        shot = out_dir / f"f{n:02d}.png"
        subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-sandbox",
                        "--hide-scrollbars", f"--window-size={width},900",
                        "--virtual-time-budget=2500",
                        f"--screenshot={shot}", variant.resolve().as_uri()],
                       capture_output=True, timeout=120)
        if not shot.exists():
            raise SystemExit(f"browser produced no screenshot for frame {n}")
        shots.append(shot)
        label = f"{tab}/{theme}" + (f"  cmd#{cmd}" if cmd is not None else "")
        print(f"  frame {n + 1}/{len(FRAMES)}  {label}")
    return shots


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "dashboard.gif"))
    ap.add_argument("--page", default=str(ROOT / "dashboard" / "index.html"))
    ap.add_argument("--width", type=int, default=1200)
    ap.add_argument("--scale", type=float, default=0.5)
    ap.add_argument("--delay", type=int, default=140, help="milliseconds per frame")
    args = ap.parse_args()

    page = Path(args.page)
    if not page.exists():
        raise SystemExit(f"{page} does not exist -- run dashboard.py first")

    browser = find_browser()
    print(f"browser: {browser}")
    with tempfile.TemporaryDirectory(prefix="gifframes-") as tmp:
        shots = capture(page, args.width, Path(tmp), browser)
        print("decoding and scaling...")
        decoded = []
        for shot in shots:
            w, h, rgb = read_png(shot)
            decoded.append(downscale(w, h, rgb, args.scale))
        width, height = decoded[0][0], decoded[0][1]
        decoded = [(w, h, px) for (w, h, px) in decoded if (w, h) == (width, height)]
        print(f"  {len(decoded)} frames at {width}x{height}")
        print("building a shared palette...")
        palette = build_palette(decoded)
        print(f"  {len(palette)} colours")
        cache: dict = {}
        indexed = [index_frame(px, palette, cache) for _, _, px in decoded]
        out = Path(args.out)
        write_gif(out, width, height, indexed, palette, max(2, args.delay // 10))
        print(f"wrote {out}  ({out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
