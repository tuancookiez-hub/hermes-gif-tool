"""GIF Tool: show, edit, compress and create GIFs in Hermes.

Wiring uses only public plugin APIs: ``ctx.register_tool`` for the four tools. Every tool returns a
plain ``MEDIA:`` line so each surface (CLI, Telegram, Discord, Desktop) gets the file; Hermes Desktop
upgrades it to the GIF Tool card (``desktop/plugin.js``), which edits and exports through
``dashboard/plugin_api.py``.

- ``gif_open``    any GIF/WebP/PNG/JPEG file or https link -> an editable project, shown in chat
- ``gif_edit``    captions, trim, speed, reverse, boomerang, crop, frame timing (free, local)
- ``gif_export``  compress to a platform preset or a size target (free, local)
- ``gif_create``  a new 4-frame GIF from a prompt (one image from the user's ChatGPT plan)
"""

from __future__ import annotations

import json
from typing import Any

from . import codex as _codex
from . import core as _core
from . import fetch as _fetch

TOOLSET = "gif_tool"

_CAPTION = {"type": "string", "maxLength": _core.MAX_CAPTION}
_DELIVER = "Put the result's media_tag on its own line in your reply to deliver the file."

OPEN_SCHEMA = {
    "name": "gif_open",
    "description": (
        "Show a GIF in the chat as an editable, compressible card. `source` is a local file path or a "
        "public https link to a GIF, animated WebP, APNG, PNG or JPEG (a GIPHY or Tenor page link works "
        "too). Use this whenever the user shares or mentions a GIF they want to see, caption, trim, "
        "speed up, shrink or convert. It is free and local apart from downloading a link. Returns a "
        "project_id for gif_edit and gif_export. " + _DELIVER
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source": {"type": "string", "maxLength": 2000,
                       "description": "File path on the Hermes machine, or an https:// link."},
        },
        "required": ["source"],
    },
}

EDIT_SCHEMA = {
    "name": "gif_edit",
    "description": (
        "Edit a GIF Tool project and re-render it. Free and instant: nothing is generated or uploaded, "
        "and the original is kept, so every edit can be undone. Omitted fields keep their current "
        "values; frame numbers start at 1. " + _DELIVER
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "From gif_open or gif_create."},
            "top_text": {**_CAPTION, "description": "Top caption, drawn exactly as given ('' removes it)."},
            "bottom_text": {**_CAPTION, "description": "Bottom caption, drawn exactly as given ('' removes it)."},
            "text_size": {"type": "string", "enum": list(_core.TEXT_SIZES)},
            "speed": {"type": "number", "minimum": _core.SPEEDS[0], "maximum": _core.SPEEDS[1],
                      "description": "Playback speed multiplier: 2 is twice as fast, 0.5 half speed."},
            "trim_start": {"type": "integer", "minimum": 1, "description": "First frame to keep."},
            "trim_end": {"type": "integer", "minimum": 1, "description": "Last frame to keep."},
            "reverse": {"type": "boolean", "description": "Play backwards."},
            "boomerang": {"type": "boolean", "description": "Play forwards then backwards."},
            "crop": {"type": "string", "enum": list(_core.CROPS), "description": "Centre crop to a shape."},
            "keep_every": {"type": "integer", "minimum": 1, "maximum": 4,
                           "description": "Keep every Nth frame (2 halves the frame count; timing is kept)."},
            "delays_ms": {"type": "array", "items": {"type": "integer"},
                          "description": "Delay for every frame of the project, in ms (short GIFs only)."},
            "enabled": {"type": "array", "items": {"type": "boolean"},
                        "description": "Which frames to include, one value per frame (short GIFs only)."},
            "reset": {"type": "boolean", "description": "Undo every edit first."},
        },
        "required": ["project_id"],
    },
}

EXPORT_SCHEMA = {
    "name": "gif_export",
    "description": (
        "Compress and export a GIF: pick a preset (" + ", ".join(
            f"{k}: {v.get('note', v['label'])}" for k, v in _core.PRESETS.items() if k != "chat")
        + ") or give your own max_kb / max_px. The compressor keeps shrinking (smarter frame "
        "differences, fewer colours, smaller frame, fewer frames) until the file fits, and reports the "
        "size before and after. Give project_id, or `source` (path or https link) to compress a file in "
        "one step. Free and local. " + _DELIVER
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "From gif_open or gif_create."},
            "source": {"type": "string", "maxLength": 2000,
                       "description": "Instead of project_id: a file path or https link to open first."},
            "preset": {"type": "string", "enum": list(_core.EXPORT_PRESETS)},
            "max_kb": {"type": "number", "minimum": 20, "maximum": 40000,
                       "description": "Fit under this many kilobytes."},
            "max_px": {"type": "integer", "minimum": 32, "maximum": _core.MAX_WORK_SIDE,
                       "description": "Longest side in pixels."},
            "format": {"type": "string", "enum": ["gif", "webp"]},
        },
        "required": [],
    },
}

CREATE_SCHEMA = {
    "name": "gif_create",
    "description": (
        "Make a new looping 4-frame meme GIF from a description. Uses ONE image from the user's ChatGPT "
        "plan through their Codex sign-in and takes about 30-60 seconds, so call it once per idea, not to "
        "tweak captions or timing (use gif_edit for that, which is free). Describe the subject and art "
        "style in `prompt`; optionally say what changes in each of the 4 frames in `frames`. Captions go "
        "in top_text / bottom_text, never in the prompt: they are drawn exactly as written. " + _DELIVER
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "maxLength": 1000,
                       "description": "Subject and art style, e.g. 'a chubby orange cartoon cat, flat vector style'."},
            "frames": {"type": "array", "maxItems": 4, "items": {"type": "string", "maxLength": 200},
                       "description": "What differs in frames 1-4, e.g. ['eyes open', 'eyes half closed', "
                                      "'eyes closed, smug grin', 'winking, tongue out']."},
            "top_text": {**_CAPTION, "description": "Top caption, drawn exactly as given (keep the user's casing)."},
            "bottom_text": {**_CAPTION, "description": "Bottom caption, drawn exactly as given."},
            "delay_ms": {"type": "integer", "minimum": _core.MIN_DELAY, "maximum": _core.MAX_DELAY,
                         "description": f"Time per frame in ms (default {_core.DEFAULT_DELAY})."},
            "quality": {"type": "string", "enum": list(_codex.QUALITIES),
                        "description": "Image quality requested (default medium). The backend may override it."},
        },
        "required": ["prompt"],
    },
}

MODEL_NOTE = ("Image made by the model your ChatGPT plan serves through Codex; the Codex endpoint does "
              "not report which model version that is.")


def _err(message: str, **extra: Any) -> str:
    return json.dumps({"success": False, "error": str(message)[:600], **extra}, ensure_ascii=False)


def _size(n: int | None) -> str:
    if not n:
        return "?"
    return f"{n / 1_000_000:.1f} MB" if n >= 1_000_000 else f"{max(1, round(n / 1000))} KB"


def _result(project: dict[str, Any], path, info: dict[str, Any], **extra: Any) -> str:
    edits = project["edits"]
    return json.dumps({
        "success": True,
        "project_id": project["id"],
        "file_path": str(path),
        "media_tag": f"MEDIA:{path}",
        "format": info["format"],
        "width": info["width"],
        "height": info["height"],
        "frames": info["frames"],
        "duration_ms": info["duration_ms"],
        "bytes": info["bytes"],
        "size": _size(info["bytes"]),
        "project_frames": len(edits["delays"]),
        "captions": {c["position"]: c["text"] for c in edits["captions"] if c["text"]},
        **extra,
    }, ensure_ascii=False)


def _open_source(source: Any) -> dict[str, Any]:
    """Create a project from a path or link. Raises ``ValueError`` (user-facing)."""
    text = str(source or "").strip()
    if not text:
        raise ValueError("source is required: a file path or an https:// link")
    if text.lower().startswith(("http://", "https://")):
        try:
            data, name = _fetch.fetch_image(text)
        except _fetch.FetchError as exc:
            raise ValueError(str(exc)) from None
        return _core.create_from_image(data, name=name, origin="url")
    data, name = _core.read_local_image(text)
    return _core.create_from_image(data, name=name, origin="file")


def _guard(fn):
    """Tools must return JSON and never raise."""
    def handler(args: dict, **_: Any) -> str:
        try:
            return fn(args if isinstance(args, dict) else {})
        except (ValueError, LookupError) as exc:
            return _err(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _err(f"GIF Tool failed: {type(exc).__name__}: {exc}")
    return handler


@_guard
def open_gif(args: dict) -> str:
    project = _open_source(args.get("source"))
    path, info = _core.render_to_media(project)
    src = project["source"]
    return _result(project, path, info,
                   original={"format": src["format"], "width": src["width"], "height": src["height"],
                             "frames": src["frames"], "size": _size(src["bytes"])},
                   hint="gif_edit changes captions, trim, speed, crop; gif_export compresses for a platform.")


@_guard
def edit_gif(args: dict) -> str:
    project = _core.load(str(args.get("project_id") or ""))
    if args.get("reset"):
        _core.reset_edits(project)
    n = len(project["edits"]["delays"])
    patch: dict[str, Any] = {}
    if "top_text" in args or "bottom_text" in args:
        current = {c["position"]: c["text"] for c in project["edits"]["captions"]}
        patch["captions"] = _core.caption_list(args.get("top_text", current.get("top", "")),
                                               args.get("bottom_text", current.get("bottom", "")))
    if args.get("trim_start") is not None or args.get("trim_end") is not None:
        start, end = project["edits"]["trim"]
        first = args.get("trim_start")
        last = args.get("trim_end")
        for value in (first, last):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError("trim_start and trim_end must be whole frame numbers")
        patch["trim"] = [start if first is None else first - 1, end if last is None else min(last, n) - 1]
    for arg, key in (("text_size", "text_size"), ("speed", "speed"), ("reverse", "reverse"),
                     ("boomerang", "boomerang"), ("crop", "crop"), ("keep_every", "step"),
                     ("delays_ms", "delays"), ("enabled", "enabled")):
        if args.get(arg) is not None:
            patch[key] = args[arg]
    _core.apply_edits(project, patch)
    path, info = _core.render_to_media(project)
    return _result(project, path, info, cost="none (rendered locally)")


@_guard
def export_gif(args: dict) -> str:
    if args.get("project_id"):
        project = _core.load(str(args["project_id"]))
    elif args.get("source"):
        project = _open_source(args["source"])
    else:
        raise ValueError("give project_id, or source (a file path or https link)")
    label, spec = _core.export_spec(args.get("preset"), args.get("max_kb"), args.get("max_px"), args.get("format"))
    path, info = _core.render_to_media(project, label, spec)
    before = project["source"].get("bytes") or 0
    extra: dict[str, Any] = {"preset": label, "fits": info["fits"], "original_size": _size(before)}
    if before:
        extra["change"] = f"{(info['bytes'] - before) / before:+.0%} vs the original file"
    if info["max_bytes"]:
        extra["limit"] = _size(info["max_bytes"])
    if not info["fits"]:
        extra["warning"] = "Could not get under the limit; trim the GIF or drop frames with gif_edit, then export again."
    return _result(project, path, info, **extra)


@_guard
def create_gif(args: dict) -> str:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    frames = args.get("frames")
    if frames is not None and not (isinstance(frames, list) and all(isinstance(f, str) for f in frames)):
        raise ValueError("frames must be a list of strings")
    quality = args.get("quality") or "medium"
    delay = args.get("delay_ms", _core.DEFAULT_DELAY)
    # Validate everything before spending an image.
    captions = _core.caption_list(args.get("top_text"), args.get("bottom_text"))
    _core._clamp_delay(delay)
    if quality not in _codex.QUALITIES:
        raise ValueError(f"quality must be one of {', '.join(_codex.QUALITIES)}")
    text = _core.sheet_prompt(prompt, frames)
    try:
        sheet, meta = _codex.generate_sheet(text, quality)
    except _codex.CodexImageError as exc:
        return _err(str(exc), retryable=False)
    project = _core.create_from_sheet(sheet, prompt=prompt, sheet_prompt_text=text, delay_ms=delay, generation=meta)
    _core.apply_edits(project, {"captions": captions})
    path, info = _core.render_to_media(project)
    return _result(project, path, info, generation=meta, model_note=MODEL_NOTE,
                   hint="Use gif_edit with this project_id to change captions or timing for free.")


def register(ctx) -> None:
    for name, schema, handler in (
        ("gif_open", OPEN_SCHEMA, open_gif),
        ("gif_edit", EDIT_SCHEMA, edit_gif),
        ("gif_export", EXPORT_SCHEMA, export_gif),
        ("gif_create", CREATE_SCHEMA, create_gif),
    ):
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=handler, emoji="🎞️")


__all__ = ["register"]
