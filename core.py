"""GIF Tool core: projects, decoding, non-destructive edits, rendering and size-targeted compression.

Pure local work (Pillow only, no network). Imported by the tools (``__init__.py``) and, standalone by
file path, by the Desktop backend (``dashboard/plugin_api.py``), so this module uses absolute imports
only. Every path is resolved per call so it follows the active Hermes profile.

A project is a source image plus an edit list:
``<HERMES_HOME>/plugin-data/gif-tool/projects/<id>/{source.<ext>,project.json}``.
The source is never modified. Every preview, chat GIF and export is rendered from source + edits, so
edits are free, reversible, and never call an image model.
"""

from __future__ import annotations

import datetime
import hashlib
import io
import json
import math
import os
import re
import shutil
import threading
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageSequence

PLUGIN_ID = "gif-tool"
ID_RE = re.compile(r"^[0-9a-f]{12}$")

SHEET_GRID = (2, 2)  # rows, cols of a generated sprite sheet
MAX_SOURCE_BYTES = 40 * 1024 * 1024
MAX_FRAMES = 240  # longer sources are subsampled on import (delays are merged, so timing is kept)
MAX_WORK_SIDE = 800
MAX_WORK_PIXELS = 48_000_000  # frames * width * height held in memory while rendering
MAX_CAPTION = 120
MIN_DELAY, MAX_DELAY, DEFAULT_DELAY = 20, 5000, 400
SPEEDS = (0.25, 4.0)
CROPS = {"none": None, "square": (1, 1), "16:9": (16, 9), "9:16": (9, 16), "4:3": (4, 3)}
TEXT_SIZES = {"small": 0.75, "medium": 1.0, "large": 1.3}
CAPTION_POSITIONS = ("top", "bottom")
SOURCE_FORMATS = {"GIF": "gif", "PNG": "png", "WEBP": "webp", "JPEG": "jpg"}

# Export presets. `max_bytes` is what the compressor fits under; limits are the platforms' published
# upload caps with a margin (Discord: 10 MB per file without Nitro, 256 KB per emoji; X: 5 MB for a
# GIF posted from mobile). `chat` is the GIF the tools put in the conversation.
PRESETS: dict[str, dict[str, Any]] = {
    "chat": {"label": "Chat", "fmt": "gif", "max_side": 480, "colors": 128, "lossy": 8, "max_bytes": 6_000_000},
    "discord": {"label": "Discord", "fmt": "gif", "max_side": 640, "colors": 256, "lossy": 8,
                "max_bytes": 9_500_000, "note": "under Discord's 10 MB upload limit"},
    "discord_emoji": {"label": "Discord emoji", "fmt": "gif", "max_side": 128, "colors": 64, "lossy": 12,
                      "max_bytes": 250_000, "square": True, "note": "128 px square, under 256 KB"},
    "telegram": {"label": "Telegram", "fmt": "gif", "max_side": 512, "colors": 128, "lossy": 12,
                 "max_bytes": 8_000_000, "note": "512 px, under 8 MB"},
    "x": {"label": "X", "fmt": "gif", "max_side": 800, "colors": 256, "lossy": 8, "max_bytes": 4_900_000,
          "note": "under X's 5 MB mobile GIF limit"},
    "small": {"label": "Under 1 MB", "fmt": "gif", "max_side": 480, "colors": 128, "lossy": 16,
              "max_bytes": 1_000_000, "note": "for slow connections and email"},
    "webp": {"label": "WebP", "fmt": "webp", "max_side": 640, "colors": 256, "lossy": 0,
             "max_bytes": 5_000_000, "note": "animated WebP: much smaller, full colour"},
}
EXPORT_PRESETS = tuple(k for k in PRESETS if k != "chat")
# The card preview trades smoothness for speed on long GIFs: at most 64 frames (timing is kept).
PREVIEW = {"fmt": "gif", "max_side": 360, "colors": 128, "lossy": 8, "max_bytes": 2_500_000, "max_frames": 64}


# ---------------------------------------------------------------- storage

def projects_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir

    path = plugin_data_dir(PLUGIN_ID) / "projects"
    path.mkdir(parents=True, exist_ok=True)
    return path


def media_dir() -> Path:
    """Where rendered files go: inside the roots Hermes delivers ``MEDIA:`` files from."""
    from hermes_constants import get_hermes_home

    path = get_hermes_home() / "cache" / "generated" / "images"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _project_dir(project_id: str) -> Path:
    if not isinstance(project_id, str) or not ID_RE.fullmatch(project_id):
        raise ValueError("project_id must be 12 lowercase hex characters")
    return projects_dir() / project_id


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def save(project: dict[str, Any]) -> None:
    folder = _project_dir(project["id"])
    folder.mkdir(parents=True, exist_ok=True)
    _write_atomic(folder / "project.json", json.dumps(project, indent=1, ensure_ascii=False).encode("utf-8"))


def load(project_id: str) -> dict[str, Any]:
    path = _project_dir(project_id) / "project.json"
    if not path.is_file():
        raise LookupError(f"No GIF Tool project {project_id} in this profile")
    try:
        project = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise LookupError(f"GIF Tool project {project_id} is unreadable") from None
    if not isinstance(project, dict) or project.get("id") != project_id or "edits" not in project:
        raise LookupError(f"GIF Tool project {project_id} is unreadable")
    return project


def source_path(project: dict[str, Any]) -> Path:
    name = (project.get("source") or {}).get("file", "")
    if not re.fullmatch(r"source\.(gif|png|webp|jpg)", name or ""):
        raise LookupError("The project's source image is missing from plugin-data")
    path = _project_dir(project["id"]) / name
    if not path.is_file():
        raise LookupError("The project's source image is missing from plugin-data")
    return path


def list_projects(limit: int = 200) -> list[dict[str, Any]]:
    rows = []
    for folder in projects_dir().iterdir():
        if not ID_RE.fullmatch(folder.name):
            continue
        try:
            rows.append(load(folder.name))
        except (LookupError, OSError):
            continue
    rows.sort(key=lambda p: p.get("created", ""), reverse=True)
    return rows[:limit]


def delete_project(project_id: str) -> None:
    folder = _project_dir(project_id)
    if not (folder / "project.json").is_file():
        raise LookupError(f"No GIF Tool project {project_id} in this profile")
    with _decode_lock:
        _DECODE_CACHE.clear()
    shutil.rmtree(folder)


# ---------------------------------------------------------------- generation prompt

def sheet_prompt(subject: str, frames: list[str] | None) -> str:
    """The prompt for one 2x2 sheet. Captions are never requested from the model (we draw them)."""
    count = SHEET_GRID[0] * SHEET_GRID[1]
    subject = subject.strip()
    beats = [f.strip() for f in (frames or []) if isinstance(f, str) and f.strip()][:count]
    if beats:
        while len(beats) < count:
            beats.append(beats[-1])
        panels = " ".join(f"Panel {i + 1}: {b}." for i, b in enumerate(beats))
    else:
        panels = ("The four panels are four consecutive moments of one short, loopable action or "
                  "expression change; panel 4 should flow naturally back into panel 1.")
    return (
        "A 2x2 animation sprite sheet: four equal square panels separated by thin straight gutters, read "
        "left-to-right, then top-to-bottom. Every panel shows the SAME subject at the same size, position, "
        "framing, camera angle, colors, lighting and art style; only the described change differs. Plain, "
        "opaque, identical background in all panels. Keep the subject centred in the middle of each panel "
        "with clear empty space above and below it (about a fifth of the panel height each). "
        f"Subject: {subject}. {panels} "
        "Absolutely no text, letters, numbers, captions, speech bubbles, borders or watermarks."
    )


# ---------------------------------------------------------------- decoding

def _flatten(im: Image.Image) -> Image.Image:
    """RGB on white (sprite sheets only; the service sometimes returns a transparent RGBA sheet)."""
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        return Image.alpha_composite(Image.new("RGBA", rgba.size, (255, 255, 255, 255)), rgba).convert("RGB")
    return im.convert("RGB")


def _background(im: Image.Image) -> tuple[int, int, int]:
    w, h = im.size
    pts = [im.getpixel((x, y)) for x in (2, w // 2, w - 3) for y in (2, h - 3)]
    pts.sort(key=sum)
    return pts[len(pts) // 2]


def _content_bbox(im: Image.Image, bg: tuple[int, int, int]) -> tuple[int, int, int, int] | None:
    diff = ImageChops.difference(im, Image.new("RGB", im.size, bg)).convert("L")
    return diff.point(lambda v: 255 if v > 28 else 0).getbbox()


def slice_sheet(sheet: Image.Image) -> list[Image.Image]:
    """Cut a sprite sheet into square frames, drop gutters, and align each subject to frame 1.

    Alignment matches each frame's content bounding box (bottom-centre) to the first frame's, which
    removes the panel-to-panel jitter image models leave in sprite sheets.
    """
    rows, cols = SHEET_GRID
    w, h = sheet.size
    cw, ch = w // cols, h // rows
    inset = max(2, int(min(cw, ch) * 0.025))
    cells = []
    for r in range(rows):
        for c in range(cols):
            cell = sheet.crop((c * cw + inset, r * ch + inset, (c + 1) * cw - inset, (r + 1) * ch - inset))
            side = min(cell.size)
            left, top = (cell.width - side) // 2, (cell.height - side) // 2
            cells.append(cell.crop((left, top, left + side, top + side)))
    bg = _background(cells[0])
    ref = _content_bbox(cells[0], bg)
    out = []
    for cell in cells:
        box = _content_bbox(cell, bg)
        if ref is None or box is None:
            out.append(cell)
            continue
        dx = ((ref[0] + ref[2]) - (box[0] + box[2])) // 2
        dy = ref[3] - box[3]
        limit = cell.width // 8  # never shove a frame far: a big offset means a real pose change
        dx, dy = max(-limit, min(limit, dx)), max(-limit, min(limit, dy))
        canvas = Image.new("RGB", cell.size, bg)
        canvas.paste(cell, (dx, dy))
        out.append(canvas)
    return out


def _frame_delay(frame: Image.Image) -> int:
    try:
        ms = int(frame.info.get("duration") or 0)
    except (TypeError, ValueError):
        ms = 0
    if ms < MIN_DELAY:  # browsers play 0-10 ms frames at 100 ms
        ms = 100
    return min(MAX_DELAY, ms)


def probe(data: bytes) -> dict[str, Any]:
    """Validate image bytes and describe them. Raises ``ValueError`` with a user-facing message."""
    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError(f"the file is over the {MAX_SOURCE_BYTES // (1024 * 1024)} MB limit")
    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = im.format
            if fmt not in SOURCE_FORMATS:
                raise ValueError("only GIF, WebP, PNG/APNG and JPEG files are supported")
            width, height = im.size
            if width < 8 or height < 8 or width > 8192 or height > 8192:
                raise ValueError("the image must be between 8 and 8192 pixels on each side")
            count = int(getattr(im, "n_frames", 1) or 1)
            if count > 5000:
                raise ValueError("the animation has more than 5000 frames")
            delays = []
            for frame in ImageSequence.Iterator(im):
                delays.append(_frame_delay(frame) if count > 1 else DEFAULT_DELAY)
    except ValueError:
        raise
    except Exception:  # noqa: BLE001 -- Pillow raises many types for broken files
        raise ValueError("the file is not a readable image") from None
    step = max(1, math.ceil(count / MAX_FRAMES))
    merged = [min(MAX_DELAY, sum(delays[i:i + step])) for i in range(0, count, step)]
    return {"format": fmt, "ext": SOURCE_FORMATS[fmt], "width": width, "height": height,
            "frames": count, "step": step, "delays": merged, "bytes": len(data)}


def _work_scale(width: int, height: int, count: int, max_side: int) -> float:
    scale = min(1.0, max_side / max(width, height))
    budget = math.sqrt(MAX_WORK_PIXELS / max(1, count * width * height))
    return min(scale, budget)


_DECODE_CACHE: "OrderedDict[tuple, dict[int, Image.Image]]" = OrderedDict()
_DECODE_CACHE_PIXELS = 40_000_000  # ~160 MB of RGBA across every cached size
_decode_lock = threading.Lock()


def _cache_get(key: tuple, wanted: set[int]) -> dict[int, Image.Image] | None:
    with _decode_lock:
        frames = _DECODE_CACHE.get(key)
        if frames is None or not wanted <= frames.keys():
            return None
        _DECODE_CACHE.move_to_end(key)
        return {i: frames[i] for i in wanted}


def _cache_put(key: tuple, frames: dict[int, Image.Image]) -> None:
    with _decode_lock:
        merged = {**_DECODE_CACHE.get(key, {}), **frames}
        _DECODE_CACHE[key] = merged
        _DECODE_CACHE.move_to_end(key)

        def total() -> int:
            return sum(f.width * f.height for d in _DECODE_CACHE.values() for f in d.values())

        while len(_DECODE_CACHE) > 1 and total() > _DECODE_CACHE_PIXELS:
            _DECODE_CACHE.popitem(last=False)
        if total() > _DECODE_CACHE_PIXELS:
            _DECODE_CACHE.clear()


def decode(project: dict[str, Any], wanted: set[int], max_side: int) -> dict[int, Image.Image]:
    """RGBA frames for the base-frame indices in ``wanted``, scaled so the long side is <= max_side.

    Decoded frames are kept in a small in-memory cache (per project and size), because every preview
    of the same project needs the same frames and decoding is the slowest step. Callers must treat
    the returned images as read-only.
    """
    src = project["source"]
    path = source_path(project)
    total = len(project["edits"]["delays"])
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        raise LookupError("The project's source image is missing from plugin-data") from None
    key = (str(path), stamp, max_side)
    cached = _cache_get(key, wanted)
    if cached is not None:
        return cached
    out: dict[int, Image.Image] = {}
    if project["kind"] == "sheet":
        with Image.open(path) as im:
            frames = slice_sheet(_flatten(im))
        scale = min(1.0, max_side / frames[0].width)
        for i in range(len(frames)):
            frame = frames[i]
            if scale < 1:
                side = max(1, round(frame.width * scale))
                frame = frame.resize((side, side), Image.Resampling.LANCZOS)
            out[i] = frame.convert("RGBA")
    else:
        step = int(src.get("step", 1))
        scale = _work_scale(src["width"], src["height"], total, max_side)
        size = (max(1, round(src["width"] * scale)), max(1, round(src["height"] * scale)))
        with Image.open(path) as im:
            for n, frame in enumerate(ImageSequence.Iterator(im)):
                index, rem = divmod(n, step)
                if rem or index not in wanted:
                    continue
                rgba = frame.convert("RGBA")
                if size != rgba.size:
                    rgba = rgba.resize(size, Image.Resampling.HAMMING, reducing_gap=2.0)
                out[index] = rgba
    missing = wanted - out.keys()
    if missing:
        raise LookupError("The project's source image no longer matches its record")
    _cache_put(key, out)
    return {i: out[i] for i in wanted}


# ---------------------------------------------------------------- edits

def default_edits(delays: list[int]) -> dict[str, Any]:
    n = len(delays)
    return {
        "captions": [{"position": "top", "text": ""}, {"position": "bottom", "text": ""}],
        "text_size": "medium",
        "trim": [0, n - 1],
        "enabled": [True] * n,
        "delays": list(delays),
        "speed": 1.0,
        "step": 1,
        "reverse": False,
        "boomerang": False,
        "crop": "none",
    }


def default_base_delays(project: dict[str, Any]) -> list[int]:
    """The frame delays the source had when it was imported (what Reset goes back to)."""
    delays = project["source"].get("delays")
    n = len(project["edits"]["delays"])
    if isinstance(delays, list) and len(delays) == n:
        return list(delays)
    return [DEFAULT_DELAY] * n


def reset_edits(project: dict[str, Any]) -> dict[str, Any]:
    project["edits"] = default_edits(default_base_delays(project))
    return project


def _clamp_delay(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("frame delay must be a number of milliseconds")
    ms = int(value)
    if not MIN_DELAY <= ms <= MAX_DELAY:
        raise ValueError(f"frame delay must be between {MIN_DELAY} and {MAX_DELAY} ms")
    return ms


def _clean_captions(captions: Any) -> list[dict[str, str]]:
    if not isinstance(captions, list):
        raise ValueError("captions must be a list")
    found = {pos: "" for pos in CAPTION_POSITIONS}
    for cap in captions:
        if not isinstance(cap, dict) or cap.get("position") not in CAPTION_POSITIONS:
            raise ValueError(f"caption position must be one of {', '.join(CAPTION_POSITIONS)}")
        text = " ".join(str(cap.get("text") or "").split())
        if len(text) > MAX_CAPTION:
            raise ValueError(f"captions are limited to {MAX_CAPTION} characters")
        found[cap["position"]] = text
    return [{"position": pos, "text": found[pos]} for pos in CAPTION_POSITIONS]


def caption_list(top: Any, bottom: Any) -> list[dict[str, str]]:
    return _clean_captions([{"position": "top", "text": top or ""}, {"position": "bottom", "text": bottom or ""}])


def apply_edits(project: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Validate ``patch`` and merge it into ``project['edits']``. Raises ``ValueError`` (user-facing)."""
    if not isinstance(patch, dict):
        raise ValueError("edits must be an object")
    edits = dict(project["edits"])
    n = len(edits["delays"])
    for key, value in patch.items():
        if value is None:
            continue
        if key == "captions":
            edits[key] = _clean_captions(value)
        elif key == "text_size":
            if value not in TEXT_SIZES:
                raise ValueError(f"text_size must be one of {', '.join(TEXT_SIZES)}")
            edits[key] = value
        elif key == "trim":
            if (not isinstance(value, list) or len(value) != 2
                    or any(isinstance(v, bool) or not isinstance(v, int) for v in value)):
                raise ValueError("trim must be [first_frame, last_frame]")
            start, end = value
            if not 0 <= start <= end <= n - 1:
                raise ValueError(f"trim must stay within frames 1-{n}, first before last")
            edits[key] = [start, end]
        elif key == "enabled":
            if not isinstance(value, list) or len(value) != n:
                raise ValueError(f"enabled must list {n} values")
            edits[key] = [bool(v) for v in value]
        elif key == "delays":
            if not isinstance(value, list) or len(value) != n:
                raise ValueError(f"delays must list {n} values")
            edits[key] = [_clamp_delay(v) for v in value]
        elif key == "speed":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not SPEEDS[0] <= value <= SPEEDS[1]:
                raise ValueError(f"speed must be between {SPEEDS[0]} and {SPEEDS[1]}")
            edits[key] = round(float(value), 2)
        elif key == "step":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 4:
                raise ValueError("step (keep every Nth frame) must be 1-4")
            edits[key] = value
        elif key in ("reverse", "boomerang"):
            edits[key] = bool(value)
        elif key == "crop":
            if value not in CROPS:
                raise ValueError(f"crop must be one of {', '.join(CROPS)}")
            edits[key] = value
        else:
            raise ValueError(f"unknown edit: {key}")
    start, end = edits["trim"]
    if not any(edits["enabled"][start:end + 1]):
        raise ValueError("keep at least one frame inside the trim range")
    project["edits"] = edits
    return project


def timeline(edits: dict[str, Any], extra_step: int = 1) -> list[tuple[int, int]]:
    """The frames that play, as ``(base_frame_index, delay_ms)``, after every timing edit."""
    start, end = edits["trim"]
    seq = [(i, edits["delays"][i]) for i in range(start, end + 1) if edits["enabled"][i]]
    step = max(1, int(edits.get("step", 1))) * max(1, extra_step)
    if step > 1 and len(seq) > 2:
        seq = [(seq[i][0], sum(d for _, d in seq[i:i + step])) for i in range(0, len(seq), step)]
    speed = float(edits.get("speed", 1.0)) or 1.0
    seq = [(i, max(MIN_DELAY, min(MAX_DELAY, int(round(d / speed / 10.0)) * 10))) for i, d in seq]
    if edits.get("reverse"):
        seq.reverse()
    if edits.get("boomerang") and len(seq) > 2:
        seq = seq + seq[-2:0:-1]
    return seq


# ---------------------------------------------------------------- captions

_FONT_CANDIDATES = (
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "impact.ttf",
    Path("/System/Library/Fonts/Supplemental/Impact.ttf"),
    Path("/Library/Fonts/Impact.ttf"),
    Path("/usr/share/fonts/truetype/msttcorefonts/Impact.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    Path("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
)
_font_cache: dict[tuple[str, int], Any] = {}


def font_path() -> Path | None:
    for cand in _FONT_CANDIDATES:
        if cand.is_file():
            return cand
    return None


def _font(size: int):
    path = font_path()
    key = (str(path), size)
    if key not in _font_cache:
        if len(_font_cache) > 64:
            _font_cache.clear()
        _font_cache[key] = ImageFont.truetype(str(path), size) if path else ImageFont.load_default(size=size)
    return _font_cache[key]


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: float) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _fit(draw, text: str, width: int, height: int, scale: float):
    """Largest font where the text fits the width in at most 2 lines and ~a quarter of the height."""
    max_w = width * 0.92
    size = max(12, int(min(width, height * 1.4) / 7 * scale))
    while size > 10:
        font = _font(size)
        lines = _wrap(draw, text, font, max_w)
        widest = max((draw.textlength(line, font=font) for line in lines), default=0)
        if len(lines) <= 2 and widest <= max_w and size * 1.1 * len(lines) <= height * 0.27 * max(1.0, scale):
            return font, size, lines
        size -= 2
    font = _font(10)
    return font, 10, _wrap(draw, text, font, max_w)[:3]


def caption_overlay(size: tuple[int, int], edits: dict[str, Any]) -> Image.Image | None:
    """One transparent layer with every caption, composited onto each frame (drawn once per render).

    Classic meme style: white fill, black outline, centred, exactly the given text.
    """
    caps = [c for c in edits.get("captions", []) if c.get("text")]
    if not caps:
        return None
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    scale = TEXT_SIZES.get(edits.get("text_size", "medium"), 1.0)
    width, height = size
    for cap in caps:
        font, fsize, lines = _fit(draw, cap["text"], width, height, scale)
        stroke = max(1, fsize // 13)
        line_h = int(fsize * 1.08)
        margin = max(4, height // 40)
        y = margin if cap["position"] == "top" else height - line_h * len(lines) - margin - stroke
        for line in lines:
            tw = draw.textlength(line, font=font)
            draw.text(((width - tw) / 2, y), line, font=font, fill=(255, 255, 255, 255),
                      stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
            y += line_h
    return layer


# ---------------------------------------------------------------- encoding

def _has_alpha(frames: list[Image.Image]) -> bool:
    return any(f.getextrema()[3][0] < 128 for f in frames)


def _palette_image(frames: list[Image.Image], colors: int) -> Image.Image:
    """A ``P`` image whose palette holds ``colors`` colours shared by every frame (no palette flicker)."""
    picks = frames if len(frames) <= 8 else [frames[round(i * (len(frames) - 1) / 7)] for i in range(8)]
    w, h = picks[0].size
    shrink = min(1.0, 144 / max(w, h))
    tw, th = max(1, round(w * shrink)), max(1, round(h * shrink))
    strip = Image.new("RGB", (tw * len(picks), th))
    for i, frame in enumerate(picks):
        strip.paste(frame.convert("RGB").resize((tw, th), Image.Resampling.BILINEAR), (i * tw, 0))
    return strip.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)


def encode_gif(frames: list[Image.Image], delays: list[int], colors: int = 128, lossy: int = 0) -> bytes:
    """Encode RGBA frames as a looping GIF.

    Compression beyond Pillow's defaults:
    - one shared palette, no dithering (dither noise defeats LZW);
    - opaque animations: pixels that did not change since the previous frame become transparent, so
      static backgrounds cost almost nothing. ``lossy`` (0-40) is how close a pixel must be to what
      is already on screen to count as unchanged; it is compared against the displayed image, so
      error never accumulates;
    - animations with real transparency keep full frames (disposal 2) so nothing smears.
    """
    colors = max(4, min(256, colors))
    alpha = _has_alpha(frames)
    pal_img = _palette_image(frames, colors - 1)  # one slot is reserved for transparency
    trans = colors - 1
    palette = (pal_img.getpalette() or [])[:trans * 3]
    palette += [0] * (colors * 3 - len(palette))
    out: list[Image.Image] = []
    canvas: Image.Image | None = None
    for frame in frames:
        rgb = frame.convert("RGB")
        pal = rgb.quantize(palette=pal_img, dither=Image.Dither.NONE)
        pal.putpalette(palette)
        if alpha:
            mask = frame.getchannel("A").point(lambda v: 255 if v < 128 else 0)
            pal.paste(trans, mask=mask)
        elif canvas is not None:
            shown = pal.convert("RGB")  # what this frame would put on screen
            diff = ImageChops.difference(shown, canvas)
            r, g, b = diff.split()
            delta = ImageChops.lighter(ImageChops.lighter(r, g), b)
            same = delta.point(lambda v: 255 if v <= lossy else 0)
            canvas.paste(shown, mask=ImageChops.invert(same))
            pal.paste(trans, mask=same)
        else:
            canvas = pal.convert("RGB")
        out.append(pal)
    buf = io.BytesIO()
    params: dict[str, Any] = {"format": "GIF", "transparency": trans, "optimize": False}
    if len(out) == 1:
        if not alpha:
            params.pop("transparency")
        out[0].save(buf, **params)
    else:
        out[0].save(buf, save_all=True, append_images=out[1:], duration=delays, loop=0,
                    disposal=2 if alpha else 1, **params)
    return buf.getvalue()


def encode_webp(frames: list[Image.Image], delays: list[int], quality: int = 75) -> bytes:
    buf = io.BytesIO()
    if len(frames) == 1:
        frames[0].save(buf, format="WEBP", quality=quality, method=2)
    else:
        frames[0].save(buf, format="WEBP", save_all=True, append_images=frames[1:], duration=delays,
                       loop=0, quality=quality, method=2)
    return buf.getvalue()


# ---------------------------------------------------------------- rendering

def _crop_box(size: tuple[int, int], crop: str) -> tuple[int, int, int, int] | None:
    ratio = CROPS.get(crop)
    if not ratio:
        return None
    w, h = size
    target = ratio[0] / ratio[1]
    if w / h > target:
        nw = max(1, round(h * target))
        left = (w - nw) // 2
        return (left, 0, left + nw, h)
    nh = max(1, round(w / target))
    top = (h - nh) // 2
    return (0, top, w, top + nh)


def render(project: dict[str, Any], spec: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
    """Render source + edits for ``spec`` (a preset-shaped dict); shrink until it fits ``max_bytes``.

    The compressor decodes once, then walks a quality ladder (lossy matching, smaller frame, fewer
    colours, dropped frames), sizing each step from how far over the cap the last attempt was. Long
    animations are sized on a 12-frame sample first, so a full encode usually happens once. If the
    result lands far under the cap, one pass grows the frame back towards it.
    """
    edits = project["edits"]
    crop = "square" if spec.get("square") else edits.get("crop", "none")
    fmt = spec.get("fmt", "gif")
    cap = spec.get("max_bytes")
    max_side = min(MAX_WORK_SIDE, int(spec.get("max_side", 480)))

    seq = timeline(edits)
    base = decode(project, {i for i, _ in seq}, max_side)
    box = _crop_box(next(iter(base.values())).size, crop)
    if box:
        base = {i: f.crop(box) for i, f in base.items()}
    full = next(iter(base.values())).size
    top_scale = min(1.0, max_side / max(full))

    def build(setting: dict[str, Any], sample: bool):
        seq = timeline(edits, setting["step"])
        size = (max(1, round(full[0] * setting["scale"])), max(1, round(full[1] * setting["scale"])))
        overlay = caption_overlay(size, edits)
        use = seq
        if sample:
            mid = max(0, len(seq) // 2 - 6)
            use = seq[mid:mid + 12]
        cache: dict[int, Image.Image] = {}
        frames = []
        for i, _ in use:
            if i not in cache:
                frame = base[i] if size == full else base[i].resize(size, Image.Resampling.LANCZOS)
                cache[i] = Image.alpha_composite(frame, overlay) if overlay else frame
            frames.append(cache[i])
        delays = [d for _, d in use]
        if fmt == "webp":
            data = encode_webp(frames, delays, setting["quality"])
        else:
            data = encode_gif(frames, delays, setting["colors"], setting["lossy"])
        return {"data": data, "size": size, "frames": len(frames), "duration": sum(d for _, d in seq),
                "weight": len(data) * len(seq) / len(use), "setting": dict(setting)}

    def tighten(setting: dict[str, Any], over: float, count: int) -> None:
        shrink = max(0.55, min(0.92, math.sqrt(1 / over) * 0.97))
        if fmt == "webp":
            if setting["quality"] > 50:
                setting["quality"] -= 15
            else:
                setting["scale"] *= shrink
        elif setting["lossy"] < 12:
            setting["lossy"] = 12
        elif over > 1.15 and setting["scale"] * max(full) > 96:
            setting["scale"] *= shrink
            if over > 3 and setting["colors"] > 64:
                setting["colors"] //= 2
        elif setting["colors"] > 64:
            setting["colors"] //= 2
        elif setting["lossy"] < 24:
            setting["lossy"] = 24
        elif setting["step"] == first_step and count > 12:
            setting["step"] = first_step * 2
        else:
            setting["scale"] *= 0.85

    first_step = max(1, math.ceil(len(seq) / int(spec["max_frames"]))) if spec.get("max_frames") else 1
    setting = {"scale": top_scale, "colors": int(spec.get("colors", 128)), "lossy": int(spec.get("lossy", 0)),
               "step": first_step, "quality": 80}
    long = len(seq) / first_step > 56
    if cap and long:
        for _ in range(9):
            trial = build(setting, sample=True)
            if trial["weight"] <= cap * 0.9 or max(trial["size"]) <= 48:
                break
            tighten(setting, trial["weight"] / cap, len(seq))
    best = build(setting, sample=False)
    if cap:
        for _ in range(4 if long else 9):
            if len(best["data"]) <= cap or max(best["size"]) <= 48:
                break
            tighten(setting, len(best["data"]) / cap, len(seq))
            best = build(setting, sample=False)
        used = len(best["data"]) / cap
        if used < 0.55 and setting["scale"] < top_scale:  # far under the cap: give some size back
            grown = dict(setting, scale=min(top_scale, setting["scale"] * min(1.6, math.sqrt(0.82 / used))))
            trial = build(grown, sample=False)
            if len(trial["data"]) <= cap:
                best = trial
    data = best["data"]
    info = {
        "format": fmt, "width": best["size"][0], "height": best["size"][1], "frames": best["frames"],
        "duration_ms": best["duration"], "bytes": len(data), "fits": not cap or len(data) <= cap,
        "colors": best["setting"]["colors"] if fmt == "gif" else None, "max_bytes": cap,
        "source_bytes": project["source"].get("bytes"),
    }
    return data, info


def edits_key(project: dict[str, Any]) -> str:
    blob = json.dumps([project["id"], project["edits"], PREVIEW], sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:20]


def preview(project: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
    """The small GIF the Desktop card shows. Cached per edit state in the project folder."""
    folder = _project_dir(project["id"])
    key = edits_key(project)
    gif, meta = folder / "preview.gif", folder / "preview.json"
    try:
        cached = json.loads(meta.read_text(encoding="utf-8"))
        if cached.get("key") == key and gif.is_file():
            return gif.read_bytes(), cached["info"]
    except (OSError, ValueError, KeyError):
        pass
    data, info = render(project, PREVIEW)
    _write_atomic(gif, data)
    _write_atomic(meta, json.dumps({"key": key, "info": info}).encode("utf-8"))
    return data, info


def custom_spec(max_kb: Any = None, max_px: Any = None, fmt: Any = None) -> dict[str, Any]:
    """An export spec from free-form limits. Raises ``ValueError`` (user-facing)."""
    spec = {"label": "Custom", "fmt": "gif", "max_side": 640, "colors": 256, "lossy": 8, "max_bytes": None}
    if fmt not in (None, ""):
        if fmt not in ("gif", "webp"):
            raise ValueError("format must be gif or webp")
        spec["fmt"] = fmt
    if max_px not in (None, ""):
        if isinstance(max_px, bool) or not isinstance(max_px, (int, float)) or not 32 <= max_px <= MAX_WORK_SIDE:
            raise ValueError(f"max_px must be between 32 and {MAX_WORK_SIDE}")
        spec["max_side"] = int(max_px)
    if max_kb not in (None, ""):
        if isinstance(max_kb, bool) or not isinstance(max_kb, (int, float)) or not 20 <= max_kb <= 40_000:
            raise ValueError("max_kb must be between 20 and 40000")
        spec["max_bytes"] = int(max_kb * 1000)
    return spec


def export_spec(preset: Any = None, max_kb: Any = None, max_px: Any = None, fmt: Any = None) -> tuple[str, dict]:
    if preset not in (None, "", "custom"):
        if preset not in EXPORT_PRESETS:
            raise ValueError(f"preset must be one of {', '.join(EXPORT_PRESETS)}")
        if max_kb in (None, "") and max_px in (None, "") and fmt in (None, ""):
            return preset, PRESETS[preset]
        spec = {**PRESETS[preset], **{k: v for k, v in custom_spec(max_kb, max_px, fmt).items()
                                      if (k == "max_bytes" and max_kb not in (None, ""))
                                      or (k == "max_side" and max_px not in (None, ""))
                                      or (k == "fmt" and fmt not in (None, ""))}}
        return preset, spec
    return "custom", custom_spec(max_kb, max_px, fmt)


def _slug(project: dict[str, Any]) -> str:
    words = re.findall(r"[a-z0-9]+", (project.get("title") or "gif").lower())[:5]
    return "-".join(words)[:48] or "gif"


def export_name(project: dict[str, Any], label: str, fmt: str) -> str:
    return f"{_slug(project)}-{re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-')}.{fmt}"


def render_to_media(project: dict[str, Any], label: str = "chat", spec: dict[str, Any] | None = None
                    ) -> tuple[Path, dict[str, Any]]:
    """Render, write the file where ``MEDIA:`` lines are delivered from, record it, save the project."""
    data, info = render(project, spec or PRESETS["chat"])
    project["rev"] = int(project.get("rev", 0)) + 1
    path = media_dir() / f"giftool_{project['id']}_{re.sub(r'[^a-z0-9]+', '', label.lower()) or 'x'}_r{project['rev']}.{info['format']}"
    _write_atomic(path, data)
    if label == "chat":
        project["current"] = {**info, "file": path.name}
    save(project)
    return path, info


# ---------------------------------------------------------------- creating projects

def _new_project(kind: str, data: bytes, described: dict[str, Any], *, title: str, origin: str,
                 name: str = "", extra: dict[str, Any] | None = None) -> dict[str, Any]:
    project_id = uuid.uuid4().hex[:12]
    folder = _project_dir(project_id)
    folder.mkdir(parents=True, exist_ok=True)
    file_name = f"source.{described['ext']}"
    _write_atomic(folder / file_name, data)
    project = {
        "id": project_id,
        "version": 2,
        "kind": kind,
        "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "title": " ".join(title.split())[:120] or "GIF",
        "source": {"file": file_name, "origin": origin, "name": name[:200], "format": described["format"],
                   "width": described["width"], "height": described["height"], "bytes": described["bytes"],
                   "frames": described["frames"], "step": described["step"],
                   "delays": list(described["delays"])},
        "edits": default_edits(described["delays"]),
        "rev": 0,
        "current": None,
        **(extra or {}),
    }
    save(project)
    return project


def create_from_sheet(sheet_png: bytes, *, prompt: str, sheet_prompt_text: str, delay_ms: int,
                      generation: dict[str, Any]) -> dict[str, Any]:
    described = probe(sheet_png)
    if described["format"] not in ("PNG", "JPEG", "WEBP"):
        raise ValueError("the image service returned an unexpected format")
    rows, cols = SHEET_GRID
    cell = min(described["width"] // cols, described["height"] // rows)
    described = {**described, "frames": rows * cols, "step": 1, "delays": [_clamp_delay(delay_ms)] * (rows * cols),
                 "width": cell, "height": cell}
    return _new_project("sheet", sheet_png, described, title=prompt, origin="generated",
                        extra={"prompt": prompt, "sheet_prompt": sheet_prompt_text, "generation": generation})


def create_from_image(data: bytes, *, name: str, origin: str) -> dict[str, Any]:
    described = probe(data)
    title = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", Path(name or "").name) or "GIF"
    described = {**described, "frames": len(described["delays"])}
    return _new_project("gif", data, described, title=title, origin=origin, name=name or "")


def read_local_image(path_text: str) -> tuple[bytes, str]:
    """Read an image file the user pointed at. Raises ``ValueError`` (user-facing)."""
    path = Path(os.path.expandvars(os.path.expanduser(path_text.strip().strip('"'))))
    if not path.is_file():
        raise ValueError(f"no file at {path}")
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError(f"the file is over the {MAX_SOURCE_BYTES // (1024 * 1024)} MB limit")
    return path.read_bytes(), path.name


def thumbnails(project: dict[str, Any], limit: int = 16, size: int = 64) -> list[dict[str, Any]]:
    """Up to ``limit`` evenly spaced base frames as small JPEGs: ``[{index, jpeg}]``."""
    n = len(project["edits"]["delays"])
    if limit <= 1:
        picks = [project["edits"]["trim"][0]]
    else:
        picks = list(range(n)) if n <= limit else sorted({round(i * (n - 1) / (limit - 1)) for i in range(limit)})
    frames = decode(project, set(picks), size)
    out = []
    for i in picks:
        frame = frames[i]
        flat = Image.new("RGB", frame.size, (255, 255, 255))
        flat.paste(frame, mask=frame.getchannel("A"))
        buf = io.BytesIO()
        flat.save(buf, format="JPEG", quality=70)
        out.append({"index": i, "jpeg": buf.getvalue()})
    return out


def summary(project: dict[str, Any]) -> dict[str, Any]:
    """The project fields safe and useful to show (no file paths)."""
    keep = ("id", "kind", "created", "title", "source", "edits", "rev", "current", "generation", "prompt")
    return {k: project.get(k) for k in keep if k in project}
