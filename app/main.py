"""HTTP API + web UI."""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import secrets
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from . import config as config_mod
from .recorder import FFMPEG, Supervisor
from .storage import Storage

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("homenvr")

STATIC = Path(__file__).parent / "static"
MAX_EXPORT_SECONDS = 6 * 3600


def create_app(cfg: config_mod.Config | None = None, run_recorders: bool = True) -> FastAPI:
    cfg = cfg or config_mod.load()
    storage = Storage(cfg.storage_dir)
    cfg.storage_dir.mkdir(parents=True, exist_ok=True)
    cfg.live_dir.mkdir(parents=True, exist_ok=True)
    # Layer the runtime-editable settings overlay (from the settings page) on top.
    from . import settings as settings_mod
    settings_mod.apply(cfg, settings_mod.load_overlay(settings_mod.settings_path(cfg)))
    sup = Supervisor(cfg, storage)

    from .webrtc import Go2rtc
    go2rtc = Go2rtc(cfg)
    from .clips import ClipConverter, ClipError, build_clip, delete_clip, is_auto, kept_name
    converter = ClipConverter(cfg, storage)
    sup.converter = converter

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        warm = reg = None
        if run_recorders:
            if go2rtc.enabled:
                # Live view + motion read go2rtc's restreams: their workers wait for this
                # (sup.sub_ready). Recording doesn't, so a restart loses as little as possible.
                async def register():
                    try:
                        await go2rtc.register_all()
                    finally:
                        sup.sub_ready.set()
                reg = asyncio.create_task(register())
            else:
                sup.sub_ready.set()
            await sup.start()
            converter.start()
            if go2rtc.enabled:
                warm = asyncio.create_task(go2rtc.keep_warm())
        yield
        for t in (warm, reg):
            if t:
                t.cancel()
        await converter.stop()
        await sup.stop()

    app = FastAPI(title="homenvr", lifespan=lifespan)
    app.state.cfg, app.state.storage, app.state.supervisor = cfg, storage, sup

    @app.middleware("http")
    async def no_cache_html(request: Request, call_next):
        # Without this, browsers heuristically cache index.html and keep running an old UI
        # after an upgrade. no-cache still allows cheap ETag revalidation (304).
        resp = await call_next(request)
        if request.url.path == "/" or request.url.path.endswith(".html"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    if cfg.ui_username:
        expected = "Basic " + base64.b64encode(f"{cfg.ui_username}:{cfg.ui_password}".encode()).decode()

        @app.middleware("http")
        async def basic_auth(request: Request, call_next):
            if request.url.path == "/healthz" or secrets.compare_digest(
                request.headers.get("authorization", ""), expected
            ):
                return await call_next(request)
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="homenvr"'})

    def camera_or_404(cam_id: str):
        cam = cfg.camera(cam_id)
        if not cam:
            raise HTTPException(404, "unknown camera")
        return cam

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/api/cameras")
    async def cameras():
        out = []
        for c in cfg.cameras:
            first, last = await asyncio.to_thread(storage.span, c.id)
            out.append({
                "id": c.id, "name": c.name, "channel": c.channel, "enabled": c.enabled,
                "status": sup.status(c.id),
                "first": first,
                "last": last,
                "rotation": c.rotation,
                "live": f"live/{c.id}/index.m3u8",
                "webrtc": bool(cfg.webrtc_api),
            })
        return out

    @app.post("/api/cameras/{cam_id}/webrtc")
    async def webrtc(cam_id: str, request: Request):
        """WebRTC signalling for the low-latency live view: the browser's SDP offer in,
        go2rtc's SDP answer out (go2rtc's own API stays internal)."""
        cam = camera_or_404(cam_id)
        if not go2rtc.enabled:
            raise HTTPException(404, "webrtc live view is not configured")
        offer = (await request.body()).decode(errors="replace")
        if not offer.startswith("v=0"):
            raise HTTPException(400, "expected an SDP offer")
        try:
            sdp = await asyncio.to_thread(go2rtc.answer, cam, offer)
        except Exception as e:
            log.warning("[%s] webrtc signalling failed: %s", cam_id, e)
            raise HTTPException(502, "webrtc unavailable")
        return Response(sdp, media_type="application/sdp", headers={"Cache-Control": "no-store"})

    @app.get("/api/cameras/{cam_id}/segments")
    async def segments(cam_id: str, start: float = Query(...), end: float = Query(...)):
        camera_or_404(cam_id)
        segs = await asyncio.to_thread(storage.segments, cam_id, start, end)
        return [{"name": s.path.name, "start": s.start, "end": s.end, "size": s.size,
                 "url": f"recordings/{cam_id}/{s.path.name}"} for s in segs]

    def event_store():
        """The live EventStore, or a read-only view of the DB when recorders are off."""
        if sup.events is not None:
            return sup.events
        if not cfg.events_db.exists():
            return None
        from .events import EventStore
        if getattr(app.state, "events_ro", None) is None:
            app.state.events_ro = EventStore(cfg.events_db)
        return app.state.events_ro

    @app.get("/api/cameras/{cam_id}/events")
    async def events(cam_id: str, start: float = Query(...), end: float = Query(...)):
        camera_or_404(cam_id)
        store = event_store()
        if store is None:
            return []
        return await asyncio.to_thread(store.query, cam_id, start, end)

    @app.get("/api/incidents")
    async def incidents(start: float = Query(...), end: float = Query(...),
                        filter: str = Query("all", pattern="^(all|person|vehicle|animal)$")):
        """Events of all cameras clustered into incidents, newest first, with noisy
        single-camera stretches folded into "busy" periods (see app/incidents.py)."""
        from . import incidents as inc
        from .motion import sensitivity_for
        store = event_store()
        evs = await asyncio.to_thread(store.query_all, start, end) if store else []
        thresholds = {c.id: sensitivity_for(c, cfg) for c in cfg.cameras}
        names = {c.id: c.name for c in cfg.cameras}
        out = inc.build(evs, thresholds, names, filter)
        for item in out["items"]:
            if item["kind"] == "busy":
                del item["events"]  # can be hundreds; the card only needs the bursts
        out["thresholds"] = thresholds
        out["labels_since"] = await asyncio.to_thread(store.first_labeled) if store else None
        out["detect"] = cfg.motion.detect
        return out

    @app.post("/api/incidents/dismiss")
    async def dismiss(request: Request):
        """Mark events as a false alarm (hidden from incidents); {"event_ids": [...], "undo": false}."""
        body = await request.json()
        ids = [int(i) for i in body.get("event_ids", [])][:20000]
        store = event_store()
        if store is None:
            raise HTTPException(400, "motion events are disabled")
        n = await asyncio.to_thread(store.dismiss, ids, not body.get("undo"))
        return {"updated": n}

    @app.get("/api/timeline")
    async def timeline(start: float = Query(...), end: float = Query(...),
                       bucket: float = Query(1800, ge=60, le=86400)):
        """Per camera: motion counts per bucket (default 30 min), recorded spans, and the
        events themselves (for marks). Built from events.db and segment names only."""
        if end <= start or end - start > 7 * 86400:
            raise HTTPException(400, "bad range")
        import math
        store = event_store()
        evs = await asyncio.to_thread(store.query_all, start, end) if store else []
        nb = math.ceil((end - start) / bucket)
        now = time.time()
        lanes = []
        for c in cfg.cameras:
            counts, person, vehicle, animal = [0] * nb, [0] * nb, [0] * nb, [0] * nb
            marks = []
            for e in evs:
                if e["camera"] != c.id:
                    continue
                i = min(nb - 1, int((e["ts"] - start) // bucket))
                counts[i] += 1
                kind = next((k for k in ("person", "vehicle", "animal") if k in e["label"]), "motion")
                if kind == "person":
                    person[i] += 1
                elif kind == "vehicle":
                    vehicle[i] += 1
                elif kind == "animal":
                    animal[i] += 1
                marks.append([round(e["ts"], 2), kind, e["id"]])
            cov = await asyncio.to_thread(storage.coverage, c.id, start, end, cfg.segment_seconds, now)
            lanes.append({"id": c.id, "name": c.name, "rotation": c.rotation,
                          "counts": counts, "person": person, "vehicle": vehicle, "animal": animal,
                          "total": len(marks), "events": marks,
                          "coverage": [[round(a, 2), round(b, 2)] for a, b in cov]})
        return {"start": start, "end": end, "bucket": bucket, "now": now, "cameras": lanes}

    @app.get("/api/cameras/{cam_id}/boxes")
    async def boxes(cam_id: str, start: float = Query(...), end: float = Query(...)):
        """What moved, for drawing over review playback: the burst frames (2 per second,
        ~10 s around each event) with their detection and motion boxes."""
        camera_or_404(cam_id)
        if end <= start or end - start > 3600:
            raise HTTPException(400, "bad range")
        store = event_store()
        return await asyncio.to_thread(store.box_frames, cam_id, start, end) if store else []

    @app.get("/api/events/{event_id}/thumb")
    async def event_thumb(event_id: int):
        """The event's saved frame; for older events (before thumbnails were persisted)
        extract one from the recording once and keep it."""
        store = event_store()
        ev = await asyncio.to_thread(store.get, event_id) if store else None
        if not ev:
            raise HTTPException(404)
        p = storage.thumb_path(ev["camera"], event_id)
        if not await asyncio.to_thread(p.is_file):
            cam = cfg.camera(ev["camera"])
            if not cam:
                raise HTTPException(404)
            from .recorder import save_thumb, thumbnail
            # Events can fall in the short gaps between recording files: use the
            # nearest footage within a few seconds.
            path = None
            for d in (0, 2, -2, 5, -5, 10, -10, 15):
                path, offset = await asyncio.to_thread(storage.segment_at, cam.id, ev["ts"] + d)
                if path:
                    break
            if not path:
                raise HTTPException(404, "no recording at that time")
            try:
                img = await thumbnail(path, offset, width=480, rot=cam.rotation)
            except Exception as e:
                raise HTTPException(502, f"thumbnail failed: {e}")
            await asyncio.to_thread(save_thumb, p, img)
            return Response(img, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

    @app.get("/api/cameras/{cam_id}/playback.mp4")
    async def playback(cam_id: str, start: float = Query(...), dur: float = Query(300),
                       height: int = Query(720, ge=240, le=1440), file: bool = Query(False)):
        """Transcode a window [start, start+dur) to H.264 and stream it progressively
        (fragmented MP4), so HEVC recordings are playable in any browser. Grid tiles
        ask for a smaller height (480), which roughly halves the CPU cost."""
        cam = camera_or_404(cam_id)
        dur = min(max(dur, 1), 1800)
        script = storage.concat_list(cam_id, start, start + dur)
        if script.count("\nfile ") == 0:
            raise HTTPException(404, "no recordings at that time")
        from .recorder import rotation_vf
        rv = rotation_vf(cam.rotation)
        vf = (rv + "," if rv else "") + f"scale=-2:{height - height % 2}"
        # Start exactly at `start` (a concat inpoint alone starts at the keyframe before
        # it, seconds early). Within one file (the usual case) an input seek is exact,
        # also in a restart's first file; across files, decode from the keyframe and
        # drop the lead-in.
        from .recorder import keyframe_before, window_inputs
        parts = await asyncio.to_thread(storage.window_parts, cam_id, start, start + dur)
        inputs, exact = None, []
        if len(parts) == 1:
            inputs = window_inputs(parts)[0]
        else:
            path, offset = await asyncio.to_thread(storage.segment_at, cam_id, start)
            if path:
                try:
                    kf, _c = await keyframe_before(path, offset)
                    script = storage.concat_list(cam_id, start - (offset - kf), start + dur)
                    exact = ["-ss", f"{offset - kf:.3f}"] if offset - kf > 0.01 else []
                except Exception:  # noqa: BLE001 - keyframe-aligned start is still playable
                    pass
        if file:
            return await _ranged_ffmpeg(f"pb-{cam_id}-{start:.3f}-{dur:g}-{height}", script, inputs=inputs, out_args=[
                *exact, "-t", str(dur), "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k"])
        return await _stream_ffmpeg(script, inputs=inputs, out_args=[*exact,
            # Keyframe every ~2 s: fragments (one per keyframe) are small, so the
            # browser can start playing ~2 s of encoded video in, not ~10 s.
            "-t", str(dur), "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-g", "50", "-keyint_min", "50",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k"])

    @app.get("/api/cameras/{cam_id}/keyframe")
    async def keyframe(cam_id: str, ts: float = Query(...)):
        """The recording keyframe at or before `ts` (plus the codec). Direct play
        stream-copies from a keyframe, so the client asks for this exact start to keep
        its clock aligned with the other cameras."""
        camera_or_404(cam_id)
        from .recorder import keyframe_before
        path, offset = await asyncio.to_thread(storage.segment_at, cam_id, ts)
        if not path:
            raise HTTPException(404, "no recording at that time")
        seg_start = ts - offset
        try:
            kf, codec = await keyframe_before(path, offset)
        except Exception as e:
            raise HTTPException(502, f"probe failed: {e}")
        return {"ts": seg_start + kf, "codec": codec}

    @app.get("/api/cameras/{cam_id}/remux.mp4")
    async def remux(cam_id: str, start: float = Query(...), dur: float = Query(300), file: bool = Query(False)):
        """Direct play: the original 2K stream, stream-copied (no re-encode, ~0 CPU) into
        fragmented MP4. Not rotated — the client rotates it like live view. Audio (AAC)
        is copied as-is.

        `start` is a keyframe (the client asks /keyframe first). The window stops at the
        end of the recording file it starts in (the client goes on with the next one):
        the file is opened with an input seek, which lands exactly on that keyframe —
        a concat inpoint, the only way to copy across files, lands seconds early in a
        restart's first file, which put the picture out of step with the clock and the
        detection boxes."""
        camera_or_404(cam_id)
        dur = min(max(dur, 1), 1800)
        parts = await asyncio.to_thread(storage.window_parts, cam_id, start, start + dur)
        if not parts:
            raise HTTPException(404, "no recordings at that time")
        from .recorder import _video_tag, probe_file_codec
        path, off, length = parts[0]
        codec = await probe_file_codec(str(path))
        # Seek a hair past the keyframe: a target that rounds to just before it lands
        # on the previous keyframe, and the copy then begins there with negative
        # timestamps that only an edit list hides — which browsers playing the stream
        # ignore (picture seconds behind its timestamps). make_zero starts it at 0.
        inputs = ["-ss", f"{off + 0.05:.3f}", "-i", str(path)]
        args = ["-avoid_negative_ts", "make_zero", "-t", f"{length:.3f}", "-map", "0:v:0", "-c:v", "copy", *_video_tag(codec), "-map", "0:a:0?", "-c:a", "copy"]
        if file:
            return await _ranged_ffmpeg(f"rx-{cam_id}-{start:.3f}-{dur:g}", "", args, inputs=inputs)
        return await _stream_ffmpeg("", args, inputs=inputs)

    # ---- ranged playback files (Safari / iOS) ---------------------------------
    # WebKit plays MP4 only from responses that honour HTTP Range (it probes with
    # "bytes=0-1" and gives up on a plain 200 stream), so for it the window is cut to
    # a real file first and served with Range. Built once per window (WebKit asks for
    # the same URL several times), kept a few minutes, pruned by age and total size.
    # Per app instance: a cached window must not outlive a code change or a recording
    # file's rename (its key is the start time). Older instances' caches are dropped.
    import shutil as _shutil
    for old in Path(tempfile.gettempdir()).glob("homenvr-play*"):
        _shutil.rmtree(old, ignore_errors=True)
    play_cache = Path(tempfile.mkdtemp(prefix="homenvr-play-"))
    play_locks: dict[str, asyncio.Lock] = {}

    def _prune_play_cache(keep_s: float = 600, max_bytes: float = 1.5e9) -> None:
        if not play_cache.is_dir():
            return
        files = sorted((e.stat().st_mtime, e.stat().st_size, Path(e.path)) for e in os.scandir(play_cache)
                       if e.is_file() and e.name.endswith(".mp4"))
        total, now = sum(f[1] for f in files), time.time()
        for mtime, size, p in files:
            if now - mtime < keep_s and total <= max_bytes:
                break
            p.unlink(missing_ok=True)
            total -= size

    async def _ranged_ffmpeg(key: str, script: str, out_args: list[str], inputs: list[str] | None = None) -> FileResponse:
        play_cache.mkdir(parents=True, exist_ok=True)
        out = play_cache / f"{key}.mp4"
        lock = play_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if not out.is_file():
                await asyncio.to_thread(_prune_play_cache)
                lst = play_cache / f"{key}.ffconcat"
                lst.write_text(script)
                part = play_cache / f"{key}.part"
                src = inputs or ["-safe", "0", "-f", "concat", "-i", str(lst)]
                proc = await asyncio.create_subprocess_exec(
                    FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
                    *src, *out_args, "-movflags", "+faststart", "-f", "mp4", str(part),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                _, err = await proc.communicate()
                lst.unlink(missing_ok=True)
                if proc.returncode != 0:
                    part.unlink(missing_ok=True)
                    raise HTTPException(500, err.decode(errors="replace")[-300:])
                part.replace(out)
        play_locks.pop(key, None)
        return FileResponse(out, media_type="video/mp4", headers={"Cache-Control": "no-store"})

    async def _stream_ffmpeg(script: str, out_args: list[str], inputs: list[str] | None = None) -> StreamingResponse:
        """Run ffmpeg over a concat script (or explicit `inputs`) and stream its
        fragmented-MP4 output."""
        tmpdir = Path(tempfile.mkdtemp(prefix="pb-"))
        (tmpdir / "list.ffconcat").write_text(script)
        src = inputs or ["-safe", "0", "-f", "concat", "-i", str(tmpdir / "list.ffconcat")]
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", *src, *out_args,
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )

        async def stream():
            try:
                while True:
                    chunk = await proc.stdout.read(65536)
                    if not chunk:
                        break
                    yield chunk
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                _rmtree(tmpdir)

        return StreamingResponse(stream(), media_type="video/mp4",
                                 headers={"Cache-Control": "no-store"})

    @app.get("/recordings/{cam_id}/{name}")
    async def recording(cam_id: str, name: str):
        camera_or_404(cam_id)
        p = storage.resolve(cam_id, name)
        if not p:
            raise HTTPException(404)
        # Starlette's FileResponse handles Range requests, so the browser can seek.
        return FileResponse(p, media_type="video/mp4", headers={"Cache-Control": "no-cache"})

    async def _build_clip(cam_id: str, start: float, end: float, out: Path) -> None:
        """Stream-copy the [start,end) range (active + grace) into `out`. Raises HTTPException."""
        try:
            await build_clip(storage, cam_id, start, end, out, MAX_EXPORT_SECONDS)
        except ClipError as e:
            raise HTTPException(e.status, str(e))

    @app.get("/api/cameras/{cam_id}/export")
    async def export(cam_id: str, start: float = Query(...), end: float = Query(...)):
        camera_or_404(cam_id)
        tmpdir = Path(tempfile.mkdtemp(prefix="export-"))
        try:
            await _build_clip(cam_id, start, end, tmpdir / "clip.mp4")
        except HTTPException:
            _rmtree(tmpdir); raise
        fname = f"{cam_id}-{time.strftime('%Y%m%d-%H%M%S', time.localtime(start))}.mp4"
        return FileResponse(tmpdir / "clip.mp4", media_type="video/mp4", filename=fname,
                            background=BackgroundTask(_rmtree, tmpdir))

    @app.post("/api/cameras/{cam_id}/save")
    async def save_clip(cam_id: str, request: Request):
        camera_or_404(cam_id)
        body = await request.json()
        start, end = float(body["start"]), float(body["end"])
        label = re.sub(r"[^A-Za-z0-9_-]+", "-", str(body.get("name", "")).strip())[:40]
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(start))
        fname = f"{cam_id}-{stamp}{('-' + label) if label else ''}.mp4"
        saved = storage.saved_dir() / fname
        part = saved.with_suffix(".part")      # build in saved/ so the rename stays on one filesystem
        try:
            await _build_clip(cam_id, start, end, part)
            part.replace(saved)                 # atomic within saved/
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        converter.remove(saved)                 # a re-save under the same name
        converter.enqueue(saved)
        try:
            await _saved_thumb(saved)
        except Exception as e:  # the clip is saved; a missing thumbnail is regenerated on view
            log.warning("saved thumbnail failed for %s: %s", fname, e)
        return {"saved": fname, "size": saved.stat().st_size}

    async def _saved_thumb(clip: Path) -> Path:
        """<clip>.jpg beside a saved clip, from ~10 s in (incident clips start with a
        10 s lead-in), rotated like the camera. Built once."""
        jpg = clip.with_suffix(".jpg")
        if not jpg.is_file():
            from .recorder import save_thumb, thumbnail
            cam = cfg.camera(clip.name.split("-", 1)[0])
            img = await thumbnail(clip, 10, width=480, rot=cam.rotation if cam else 0)
            await asyncio.to_thread(save_thumb, jpg, img)
        return jpg

    @app.get("/api/saved")
    async def list_saved():
        d = storage.saved_dir()
        import os as _os
        out = [{"name": e.name, "size": e.stat().st_size, "ctime": e.stat().st_mtime,
                "url": f"saved/{e.name}", "seekable": converter.ready(Path(e.path)) is not None,
                "auto": is_auto(e.name)}
               for e in _os.scandir(d) if e.is_file() and e.name.endswith(".mp4")]
        out.sort(key=lambda x: x["ctime"], reverse=True)
        return out

    def _safe_saved(name: str) -> Path | None:
        if "/" in name or "\\" in name or name.startswith(".") or not name.endswith(".mp4"):
            return None
        p = storage.saved_dir() / name
        return p if p.is_file() else None

    @app.get("/saved/{name}")
    async def serve_saved(name: str):
        p = _safe_saved(name)
        if not p:
            raise HTTPException(404)
        return FileResponse(p, media_type="video/mp4")

    @app.get("/api/saved/{name}/thumb")
    async def saved_thumb(name: str):
        p = _safe_saved(name)
        if not p:
            raise HTTPException(404)
        try:
            jpg = await _saved_thumb(p)
        except Exception as e:
            raise HTTPException(502, f"thumbnail failed: {e}")
        return FileResponse(jpg, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

    @app.get("/api/saved/{name}/play.mp4")
    async def saved_play(name: str, height: int = Query(720, ge=240, le=1440)):
        """A saved clip as H.264 (rotated) for the page's player: the seekable copy
        (served with Range) once it exists, else a live transcode meanwhile. Download
        still gives the untouched original."""
        p = _safe_saved(name)
        if not p:
            raise HTTPException(404)
        ready = converter.ready(p)
        if ready:
            return FileResponse(ready, media_type="video/mp4")
        converter.enqueue(p)
        from .recorder import rotation_vf
        cam = cfg.camera(name.split("-", 1)[0])
        rv = rotation_vf(cam.rotation if cam else 0)
        vf = (rv + "," if rv else "") + f"scale=-2:{height - height % 2}"
        script = f"ffconcat version 1.0\nfile '{p.as_posix()}'\n"
        return await _stream_ffmpeg(script, [
            "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k"])

    @app.delete("/api/saved/{name}")
    async def delete_saved(name: str):
        p = _safe_saved(name)
        if not p:
            raise HTTPException(404)
        delete_clip(p.parent, name)
        return {"deleted": name}

    @app.post("/api/saved/{name}/keep")
    async def keep_saved(name: str):
        """Make an auto-saved clip permanent (out of the autosave limits)."""
        p = _safe_saved(name)
        if not p or not is_auto(name):
            raise HTTPException(404)
        dst = p.with_name(kept_name(name))
        if dst.exists():
            raise HTTPException(409, "a kept clip with that name exists")
        for a, b in ((p, dst), (p.with_suffix(".jpg"), dst.with_suffix(".jpg")),
                     (converter.play_path(p), converter.play_path(dst))):
            if a.exists():
                a.rename(b)
        return {"kept": dst.name}

    @app.get("/api/server-config")
    async def server_config():
        """config.yml and the environment it reads (.env), secrets masked — for the
        read-only card on the Settings page."""
        from . import settings as s
        from .serverinfo import describe
        overlay = await asyncio.to_thread(s.load_overlay, s.settings_path(cfg))
        return await asyncio.to_thread(describe, None, cfg, overlay)

    @app.post("/api/restart")
    async def restart():
        """Exit; Docker's restart policy (unless-stopped) starts homenvr again. The
        recorders are stopped cleanly first (SIGTERM -> lifespan shutdown)."""
        import signal
        log.warning("restart requested from the settings page")
        asyncio.get_running_loop().call_later(0.5, os.kill, os.getpid(), signal.SIGTERM)
        return {"restarting": True}

    usage_cache: dict = {"t": 0.0, "val": None, "task": None}

    async def cached_usage() -> dict:
        """storage.usage() stats every segment file (seconds on a big archive), so
        serve a cached copy and refresh it in the background once it's a minute old."""
        async def refresh():
            usage_cache["val"] = await asyncio.to_thread(storage.usage)
            usage_cache["t"] = time.monotonic()
        stale = time.monotonic() - usage_cache["t"] > 60
        if usage_cache["val"] is None:
            await refresh()
        elif stale and (usage_cache["task"] is None or usage_cache["task"].done()):
            usage_cache["task"] = asyncio.create_task(refresh())
        return usage_cache["val"]

    @app.get("/api/status")
    async def status():
        r = cfg.retention
        return {"storage": await cached_usage(),
                "retention": {"max_gb": r.max_gb, "max_days": r.max_days, "min_free_gb": r.min_free_gb},
                "segment_seconds": cfg.segment_seconds,
                "alerts": {"notifier": cfg.alerts.notifier},
                "motion": {"enabled": cfg.motion.enabled}}

    @app.get("/api/health")
    async def health():
        from .alerts import build_observer
        checks = await asyncio.to_thread(build_observer(cfg, sup, storage), time.time())
        return {"ok": all(c.ok for c in checks),
                "checks": [{"key": c.key, "ok": c.ok, "message": c.message} for c in checks]}

    @app.post("/api/alerts/test")
    async def alerts_test():
        from .alerts import make_notifier
        n = make_notifier(cfg.alerts)
        from .alerts import NullNotifier
        try:
            await asyncio.to_thread(n.send, "homenvr test alert",
                                    "This is a test notification from homenvr.", failing=False)
        except Exception as e:
            raise HTTPException(502, f"notifier failed: {e}")
        return {"sent": True, "notifier": cfg.alerts.notifier,
                "delivered": not isinstance(n, NullNotifier)}

    @app.get("/api/config")
    async def get_config():
        from . import settings as s
        return {"settings": s.current(cfg),
                "cameras": [{"id": c.id, "name": c.name, "channel": c.channel} for c in cfg.cameras],
                "segment_seconds": cfg.segment_seconds,
                "notifiers": ["none", "ntfy", "webhook", "telegram", "smtp"]}

    @app.post("/api/config")
    async def post_config(request: Request):
        from . import settings as s
        patch = await request.json()
        try:
            result = await asyncio.to_thread(s.update, cfg, s.settings_path(cfg), patch)
        except ValueError as e:
            raise HTTPException(400, str(e))
        sup.reload_notifier()  # apply alert changes live
        for cam_id, fields in (patch.get("cameras") or {}).items():
            if isinstance(fields, dict) and ("zones" in fields or "rotation" in fields) and cam_id in sup.motion:
                sup.motion[cam_id].restart()  # rebuild its motion mask
        return {"settings": result}

    @app.get("/api/cameras/{cam_id}/snapshot")
    async def camera_snapshot(cam_id: str):
        cam = camera_or_404(cam_id)
        from .recorder import snapshot_camera
        try:
            img = await snapshot_camera(cam, cfg)
        except Exception as e:
            raise HTTPException(502, f"snapshot failed: {e}")
        return Response(img, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})

    @app.post("/api/cameras/{cam_id}/snapshot/push")
    async def push_snapshot(cam_id: str):
        cam = camera_or_404(cam_id)
        from .alerts import NullNotifier, make_notifier
        from .recorder import snapshot_camera
        n = make_notifier(cfg.alerts)
        if isinstance(n, NullNotifier):
            raise HTTPException(400, "no notifier configured — set one in Settings first")
        try:
            img = await snapshot_camera(cam, cfg)
            when = time.strftime("%H:%M:%S", time.localtime())
            await asyncio.to_thread(n.send_image, f"Snapshot — {cam.name}",
                                    f"Manual snapshot at {when}", img, f"{cam_id}-{int(time.time())}.jpg")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"push failed: {e}")
        return {"pushed": True, "notifier": cfg.alerts.notifier}

    @app.get("/api/cameras/{cam_id}/thumb")
    async def thumb(cam_id: str, ts: float = Query(...)):
        cam = camera_or_404(cam_id)
        from .recorder import thumbnail
        path, offset = await asyncio.to_thread(storage.segment_at, cam_id, ts)
        if not path:
            raise HTTPException(404, "no recording at that time")
        try:
            img = await thumbnail(path, offset, rot=cam.rotation)
        except Exception as e:
            raise HTTPException(502, f"thumbnail failed: {e}")
        return Response(img, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

    @app.get("/live/{cam_id}/{name}")
    async def live(cam_id: str, name: str):
        camera_or_404(cam_id)
        if "/" in name or name.startswith("."):
            raise HTTPException(404)
        p = cfg.live_dir / cam_id / name
        if not p.is_file():
            raise HTTPException(404)
        mt = "application/vnd.apple.mpegurl" if name.endswith(".m3u8") else "video/mp4"
        return FileResponse(p, media_type=mt, headers={"Cache-Control": "no-cache"})

    @app.exception_handler(ValueError)
    async def value_error(_r, e):
        return JSONResponse({"detail": str(e)}, status_code=400)

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app


def _rmtree(p: Path) -> None:
    for f in p.iterdir():
        f.unlink(missing_ok=True)
    p.rmdir()


def app_factory() -> FastAPI:
    return create_app()
