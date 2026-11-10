"""Offline tests for the local pipeline (no network, no Hermes home)."""

import importlib.util
import io
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageSequence

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gif_tool_core", ROOT / "core.py")
core = importlib.util.module_from_spec(spec)
sys.modules["gif_tool_core"] = core
spec.loader.exec_module(core)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    (tmp_path / "projects").mkdir()
    (tmp_path / "media").mkdir()
    monkeypatch.setattr(core, "projects_dir", lambda: tmp_path / "projects")
    monkeypatch.setattr(core, "media_dir", lambda: tmp_path / "media")
    return tmp_path


def sheet_png(offsets=((0, 0), (20, 0), (0, 15), (-10, 5)), transparent=False) -> bytes:
    im = Image.new("RGB", (1000, 1000), "white")
    d = ImageDraw.Draw(im)
    d.line((500, 0, 500, 1000), fill=(200, 200, 200), width=4)
    d.line((0, 500, 1000, 500), fill=(200, 200, 200), width=4)
    for i, (dx, dy) in enumerate(offsets):
        ox, oy = (i % 2) * 500, (i // 2) * 500
        d.rectangle((ox + 150 + dx, oy + 150 + dy, ox + 350 + dx, oy + 350 + dy), fill=(220, 30, 30))
        d.ellipse((ox + 220 + dx, oy + 200 + dy, ox + 280 + dx, oy + 260 + dy), fill=(30, 30 + 50 * i, 200))
    if transparent:
        im = im.convert("RGBA")
        px = im.load()
        for x in range(im.width):
            for y in range(im.height):
                if px[x, y][:3] == (255, 255, 255):
                    px[x, y] = (200, 0, 0, 0)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def anim_frames(n=12, size=(200, 120), alpha=False):
    """A ball crossing a static gradient (or a transparent canvas)."""
    w, h = size
    bg = Image.linear_gradient("L").resize(size).convert("RGB")
    frames = []
    for i in range(n):
        im = Image.new("RGBA", size, (0, 0, 0, 0)) if alpha else bg.convert("RGBA")
        x = 10 + i * (w - 50) // max(1, n - 1)
        ImageDraw.Draw(im).ellipse((x, 40, x + 30, 70), fill=(255, 40, 40, 255))
        frames.append(im)
    return frames


def gif_bytes(n=12, size=(200, 120), alpha=False, delay=80) -> bytes:
    frames = anim_frames(n, size, alpha)
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=delay, loop=0,
                   disposal=2 if alpha else 1)
    return buf.getvalue()


def decoded(data: bytes):
    im = Image.open(io.BytesIO(data))
    return [(f.convert("RGBA").copy(), f.info.get("duration")) for f in ImageSequence.Iterator(im)], im.info


def maxdiff(a: Image.Image, b: Image.Image) -> int:
    return max(hi for _, hi in ImageChops.difference(a.convert("RGB"), b.convert("RGB")).getextrema())


def sheet_project(**kw):
    return core.create_from_sheet(sheet_png(**kw), prompt="red square", sheet_prompt_text="x", delay_ms=300,
                                  generation={})


def gif_project(**kw):
    return core.create_from_image(gif_bytes(**kw), name="ball.gif", origin="file")


# ---------------------------------------------------------------- encoder

def test_delta_encoding_roundtrips_exactly_when_lossless():
    frames = anim_frames(10)
    data = core.encode_gif(frames, [50] * 10, colors=256, lossy=0)
    out, info = decoded(data)
    assert len(out) == 10 and info.get("loop") == 0
    assert [d for _, d in out] == [50] * 10
    pal = core._palette_image(frames, 255)
    for (got, _), want in zip(out, frames):
        expect = want.convert("RGB").quantize(palette=pal, dither=Image.Dither.NONE).convert("RGB")
        assert maxdiff(got, expect) == 0


def test_lossy_error_never_accumulates():
    frames = anim_frames(24)
    out, _ = decoded(core.encode_gif(frames, [40] * 24, colors=64, lossy=20))
    pal = core._palette_image(frames, 63)
    for (got, _), want in zip(out, frames):
        expect = want.convert("RGB").quantize(palette=pal, dither=Image.Dither.NONE).convert("RGB")
        assert maxdiff(got, expect) <= 20


def test_delta_encoding_is_smaller_than_plain_pillow():
    frames = anim_frames(30, (320, 200))
    plain = io.BytesIO()
    frames[0].convert("RGB").save(plain, format="GIF", save_all=True,
                                  append_images=[f.convert("RGB") for f in frames[1:]], duration=40, loop=0)
    ours = core.encode_gif(frames, [40] * 30, colors=128, lossy=8)
    assert len(ours) < len(plain.getvalue())


def test_transparent_animation_keeps_transparency_without_smearing():
    frames = anim_frames(6, alpha=True)
    out, _ = decoded(core.encode_gif(frames, [60] * 6, colors=64))
    for (got, _), want in zip(out, frames):
        assert got.getpixel((2, 2))[3] == 0
        visible = sum(got.getchannel("A").histogram()[1:])
        expected = sum(want.getchannel("A").histogram()[128:])
        assert visible == expected  # only this frame's ball, never a trail


def test_single_frame_and_webp():
    one = anim_frames(1)
    assert len(decoded(core.encode_gif(one, [100]))[0]) == 1
    webp = core.encode_webp(anim_frames(5), [70] * 5)
    assert Image.open(io.BytesIO(webp)).n_frames == 5


# ---------------------------------------------------------------- projects

def test_sheet_slices_align_and_flatten(home):
    p = sheet_project()
    frames = core.decode(p, {0, 1, 2, 3}, 400)
    boxes = [core._content_bbox(f.convert("RGB"), (255, 255, 255)) for f in frames.values()]
    assert len({(b[0] + b[2], b[3]) for b in boxes}) == 1, boxes
    t = sheet_project(transparent=True)
    assert core.decode(t, {0}, 200)[0].getpixel((3, 3)) == (255, 255, 255, 255)


def test_import_gif_and_render_chat(home):
    p = gif_project(n=12, delay=80)
    assert p["kind"] == "gif" and p["source"]["frames"] == 12 and p["edits"]["delays"] == [80] * 12
    path, info = core.render_to_media(p)
    assert path.is_file() and info["frames"] == 12 and info["fits"]
    out, meta = decoded(path.read_bytes())
    assert meta.get("loop") == 0 and len(out) == 12
    assert core.load(p["id"])["current"]["file"] == path.name


def test_long_gif_is_subsampled_with_timing_kept(home, monkeypatch):
    monkeypatch.setattr(core, "MAX_FRAMES", 10)
    p = gif_project(n=25, delay=40)
    assert p["source"]["step"] == 3 and len(p["edits"]["delays"]) == 9
    assert sum(p["edits"]["delays"]) == 25 * 40
    _, info = core.render(p, core.PRESETS["chat"])
    assert info["frames"] == 9 and info["duration_ms"] == 1000


def test_static_image_becomes_one_frame_project(home):
    buf = io.BytesIO()
    Image.new("RGB", (300, 200), (10, 120, 200)).save(buf, format="PNG")
    p = core.create_from_image(buf.getvalue(), name="pic.png", origin="file")
    core.apply_edits(p, {"captions": core.caption_list("HELLO", "")})
    data, info = core.render(p, core.PRESETS["chat"])
    assert info["frames"] == 1 and Image.open(io.BytesIO(data)).format == "GIF"


def test_probe_rejects_garbage():
    for bad in (b"", b"not an image", b"GIF89a" + b"\x00" * 20):
        with pytest.raises(ValueError):
            core.probe(bad)


# ---------------------------------------------------------------- edits

def test_timeline_edits():
    e = core.default_edits([100] * 8)
    assert core.timeline(e) == [(i, 100) for i in range(8)]
    e.update(trim=[2, 6], speed=2.0)
    assert core.timeline(e) == [(i, 50) for i in range(2, 7)]
    e.update(reverse=True)
    assert [i for i, _ in core.timeline(e)] == [6, 5, 4, 3, 2]
    e.update(reverse=False, boomerang=True)
    assert [i for i, _ in core.timeline(e)] == [2, 3, 4, 5, 6, 5, 4, 3]
    e.update(boomerang=False, speed=1.0, step=2)
    assert core.timeline(e) == [(2, 200), (4, 200), (6, 100)]
    e.update(step=1, speed=4.0)
    assert all(d == 30 or d == 20 for _, d in core.timeline(e))  # rounded to GIF centiseconds, floor 20


def test_apply_edits_validates(home):
    p = gif_project(n=6)
    core.apply_edits(p, {"trim": [1, 4], "speed": 1.5, "crop": "square", "text_size": "large",
                         "enabled": [True, True, False, True, True, True], "reverse": True})
    assert p["edits"]["trim"] == [1, 4] and p["edits"]["crop"] == "square"
    for bad in ({"trim": [4, 1]}, {"trim": [0, 6]}, {"speed": 9}, {"step": 0}, {"crop": "circle"},
                {"delays": [1] * 6}, {"enabled": [False] * 6}, {"enabled": [True]}, {"nope": 1},
                {"text_size": "huge"}, {"trim": [True, 2]},
                {"captions": [{"position": "top", "text": "x" * 200}]},
                {"captions": [{"position": "side", "text": "x"}]}):
        before = dict(p["edits"])
        with pytest.raises(ValueError):
            core.apply_edits(p, bad)
        assert p["edits"] == before, bad  # a rejected patch changes nothing


def test_trim_cannot_leave_zero_frames(home):
    p = gif_project(n=6)
    core.apply_edits(p, {"enabled": [False, False, True, True, True, True]})
    with pytest.raises(ValueError):
        core.apply_edits(p, {"trim": [0, 1]})


def test_crop_and_caption_change_output(home):
    p = gif_project(n=4, size=(240, 120))
    plain, a = core.render(p, core.PRESETS["chat"])
    assert (a["width"], a["height"]) == (240, 120)
    core.apply_edits(p, {"crop": "square", "captions": core.caption_list("TOP TEXT", "bottom text")})
    data, b = core.render(p, core.PRESETS["chat"])
    assert b["width"] == b["height"] == 120 and data != plain


def test_bad_ids_rejected(home):
    for bad in ("../etc", "ABCDEF123456", "", "123", None):
        with pytest.raises(ValueError):
            core.load(bad)
    with pytest.raises(LookupError):
        core.load("0123456789ab")


# ---------------------------------------------------------------- compression

def noisy_gif(n=24, size=(360, 360)) -> bytes:
    """Hard to compress: every pixel changes every frame."""
    import random

    rnd = random.Random(7)
    frames = []
    for _ in range(n):
        small = Image.frombytes("RGB", (45, 45), bytes(rnd.randrange(256) for _ in range(45 * 45 * 3)))
        frames.append(small.resize(size, Image.Resampling.BICUBIC))
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=60, loop=0)
    return buf.getvalue()


def test_compressor_fits_under_the_cap(home):
    p = core.create_from_image(noisy_gif(), name="noise.gif", origin="file")
    big, info_big = core.render(p, {**core.PRESETS["discord"], "max_bytes": None})
    cap = len(big) // 6
    data, info = core.render(p, {**core.PRESETS["discord"], "max_bytes": cap})
    assert info["fits"] and len(data) <= cap
    assert info["width"] < info_big["width"]
    assert Image.open(io.BytesIO(data)).n_frames >= 2


def test_presets_and_custom_specs(home):
    p = gif_project(n=8, size=(300, 200))
    for name in core.EXPORT_PRESETS:
        label, spec = core.export_spec(name)
        data, info = core.render(p, spec)
        assert info["fits"], name
        assert Image.open(io.BytesIO(data)).format == ("WEBP" if spec["fmt"] == "webp" else "GIF")
    _, emoji = core.render(p, core.PRESETS["discord_emoji"])
    assert emoji["width"] == emoji["height"] <= 128 and emoji["bytes"] <= 250_000
    label, spec = core.export_spec(None, max_kb=30, max_px=100)
    data, info = core.render(p, spec)
    assert label == "custom" and max(info["width"], info["height"]) <= 100 and len(data) <= 30_000
    for bad in ({"preset": "myspace"}, {"max_kb": 1}, {"max_px": 5000}, {"fmt": "mp4"}):
        with pytest.raises(ValueError):
            core.export_spec(**bad)


def test_preview_is_cached_per_edit_state(home, monkeypatch):
    p = gif_project(n=5)
    first, _ = core.preview(p)
    calls = []
    real = core.render
    monkeypatch.setattr(core, "render", lambda *a, **k: calls.append(1) or real(*a, **k))
    again, _ = core.preview(p)
    assert again == first and not calls
    core.apply_edits(p, {"speed": 2.0})
    core.preview(p)
    assert calls == [1]


def test_thumbnails_and_delete(home):
    p = gif_project(n=40)
    thumbs = core.thumbnails(p, limit=16)
    assert len(thumbs) == 16 and thumbs[0]["index"] == 0 and thumbs[-1]["index"] == 39
    assert [r["id"] for r in core.list_projects()] == [p["id"]]
    core.delete_project(p["id"])
    assert core.list_projects() == []
    with pytest.raises(LookupError):
        core.delete_project(p["id"])


def test_sheet_prompt_never_asks_for_text():
    text = core.sheet_prompt("a cat", ["a", "b"])
    assert "Panel 4: b." in text and "no text" in text
