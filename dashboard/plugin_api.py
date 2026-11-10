"""GIF Tool backend for the Desktop card and studio. Mounted at ``/api/plugins/gif-tool/``.

Everything here is local: it reads and re-renders the projects the tools stored, and never calls an
image model. Bytes go back as ``data:`` URLs so the card works the same when Desktop is connected to a
remote Hermes. The only network use is ``POST /import`` with a ``url``, which downloads that one link.

Routes:
    GET  /project?id=          project, preview GIF, frame thumbnails, presets, limits
    POST /preview              {id, edits?, reset?} -> save the edits, return the new preview
    POST /export               {id, preset? | max_kb?, max_px?, format?} -> compressed file to download
    GET  /projects             the profile's GIF library (newest first) with a thumbnail each
    POST /import               {name, data} (base64 file) or {url} -> a new project
    POST /delete               {id} -> remove a project from plugin storage
"""

from __future__ import annotations

import base64
import binascii
import importlib.util
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

router = APIRouter()
_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    """A module beside this folder; the plugin host imports this file standalone (no package)."""
    full = f"gif_tool_backend_{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, _ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


core = _load("core")
fetch = _load("fetch")

_MIME = {"gif": "image/gif", "webp": "image/webp"}


def _data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _project(project_id: Any) -> dict[str, Any]:
    try:
        return core.load(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


def _preview_payload(project: dict[str, Any]) -> dict[str, Any]:
    try:
        data, info = core.preview(project)
    except LookupError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from None
    return {"project": core.summary(project), "gif": _data_url(data, "image/gif"), "info": info,
            "timeline": len(core.timeline(project["edits"]))}


@router.get("/project")
def project(id: str = Query("")) -> dict:  # noqa: A002 -- the query parameter is named id
    proj = _project(id)
    payload = _preview_payload(proj)
    try:
        thumbs = [{"index": t["index"], "src": _data_url(t["jpeg"], "image/jpeg")} for t in core.thumbnails(proj)]
    except LookupError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from None
    font = core.font_path()
    return {
        **payload,
        "thumbs": thumbs,
        "presets": [{"id": k, "label": core.PRESETS[k]["label"], "note": core.PRESETS[k].get("note", "")}
                    for k in core.EXPORT_PRESETS],
        "limits": {"min_delay": core.MIN_DELAY, "max_delay": core.MAX_DELAY, "max_caption": core.MAX_CAPTION,
                   "min_speed": core.SPEEDS[0], "max_speed": core.SPEEDS[1], "max_px": core.MAX_WORK_SIDE},
        "crops": list(core.CROPS),
        "text_sizes": list(core.TEXT_SIZES),
        "font": font.stem.replace("-", " ").title() if font else "built-in",
    }


@router.post("/preview")
def preview(body: dict = Body(...)) -> dict:
    proj = _project(body.get("id"))
    try:
        if body.get("reset"):
            core.reset_edits(proj)
        if body.get("edits") is not None:
            core.apply_edits(proj, body["edits"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    core.save(proj)
    return _preview_payload(proj)


@router.post("/export")
def export(body: dict = Body(...)) -> dict:
    proj = _project(body.get("id"))
    try:
        label, spec = core.export_spec(body.get("preset"), body.get("max_kb"), body.get("max_px"), body.get("format"))
        data, info = core.render(proj, spec)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except LookupError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from None
    mime = _MIME[info["format"]]
    return {**info, "preset": label, "label": spec.get("label", "Custom"), "mime": mime,
            "file_name": core.export_name(proj, spec.get("label", "custom"), info["format"]),
            "data_url": _data_url(data, mime)}


@router.get("/projects")
def projects(limit: int = Query(60, ge=1, le=200)) -> dict:
    rows = []
    for proj in core.list_projects(limit):
        try:
            thumb = core.thumbnails(proj, limit=1, size=96)
            src = _data_url(thumb[0]["jpeg"], "image/jpeg") if thumb else None
        except (LookupError, OSError, ValueError):
            src = None
        rows.append({"id": proj["id"], "title": proj.get("title"), "created": proj.get("created"),
                     "kind": proj.get("kind"), "frames": len(proj["edits"]["delays"]),
                     "bytes": proj["source"].get("bytes"), "width": proj["source"].get("width"),
                     "height": proj["source"].get("height"), "thumb": src})
    return {"projects": rows, "max_upload_bytes": core.MAX_SOURCE_BYTES}


@router.post("/import")
def import_(body: dict = Body(...)) -> dict:
    try:
        if body.get("url"):
            try:
                data, name = fetch.fetch_image(str(body["url"]))
            except fetch.FetchError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            proj = core.create_from_image(data, name=name, origin="url")
        else:
            raw = str(body.get("data") or "")
            if "," in raw[:80]:  # a data: URL from the browser's FileReader
                raw = raw.split(",", 1)[1]
            if len(raw) > core.MAX_SOURCE_BYTES * 4 // 3 + 16:
                raise ValueError(f"the file is over the {core.MAX_SOURCE_BYTES // (1024 * 1024)} MB limit")
            try:
                data = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError):
                raise ValueError("the upload was not valid base64") from None
            proj = core.create_from_image(data, name=str(body.get("name") or "upload"), origin="upload")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"id": proj["id"]}


@router.post("/delete")
def delete(body: dict = Body(...)) -> dict:
    try:
        core.delete_project(body.get("id"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return {"deleted": True}
