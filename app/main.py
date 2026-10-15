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

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if run_recorders:
            await sup.start()
        yield
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
            })
        return out

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
                        filter: str = Query("all", pattern="^(all|person|vehicle)$")):
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
            counts, person, vehicle = [0] * nb, [0] * nb, [0] * nb
            marks = []
            for e in evs:
                if e["camera"] != c.id:
                    continue
                i = min(nb - 1, int((e["ts"] - start) // bucket))
                counts[i] += 1
                kind = "person" if "person" in e["label"] else "vehicle" if "vehicle" in e["label"] else "motion"
                if kind == "person":
                    person[i] += 1
                elif kind == "vehicle":
                    vehicle[i] += 1
                marks.append([round(e["ts"], 2), kind, e["id"]])
            cov = await asyncio.to_thread(storage.coverage, c.id, start, end, cfg.segment_seconds, now)
            lanes.append({"id": c.id, "name": c.name, "rotation": c.rotation,
                          "counts": counts, "person": person, "vehicle": vehicle,
                          "total": len(marks), "events": marks,
                          "coverage": [[round(a, 2), round(b, 2)] for a, b in cov]})
        return {"start": start, "end": end, "bucket": bucket, "now": now, "cameras": lanes}

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
                       height: int = Query(720, ge=240, le=1440)):
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
        return await _stream_ffmpeg(script, [
            "-t", str(dur), "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
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
    async def remux(cam_id: str, start: float = Query(...), dur: float = Query(300)):
        """Direct play: the original 2K stream, stream-copied (no re-encode, ~0 CPU) into
        fragmented MP4. Only for browsers that decode the camera codec (HEVC). Not
        rotated — the client rotates it like live view. No audio (the grid is muted)."""
        camera_or_404(cam_id)
        dur = min(max(dur, 1), 1800)
        script = storage.concat_list(cam_id, start, start + dur)
        if script.count("\nfile ") == 0:
            raise HTTPException(404, "no recordings at that time")
        from .recorder import _video_tag, probe_file_codec
        first = re.search(r"^file '(.+)'$", script, re.M).group(1)
        codec = await probe_file_codec(first)
        return await _stream_ffmpeg(script, ["-t", str(dur), "-map", "0:v:0", "-c:v", "copy",
                                             *_video_tag(codec), "-an"])

    async def _stream_ffmpeg(script: str, out_args: list[str]) -> StreamingResponse:
        """Run ffmpeg over a concat script and stream its fragmented-MP4 output."""
        tmpdir = Path(tempfile.mkdtemp(prefix="pb-"))
        (tmpdir / "list.ffconcat").write_text(script)
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-safe", "0",
            "-f", "concat", "-i", str(tmpdir / "list.ffconcat"), *out_args,
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

    async def _build_clip(cam_id: str, start: float, end: float, out: Path, lst: Path) -> None:
        """Stream-copy the [start,end) range (active + grace) into `out`. Raises HTTPException."""
        if end <= start or end - start > MAX_EXPORT_SECONDS:
            raise HTTPException(400, f"range must be 0-{MAX_EXPORT_SECONDS}s")
        script = storage.concat_list(cam_id, start, end)
        if script.count("\nfile ") == 0:
            raise HTTPException(404, "no recordings in range")
        lst.write_text(script)
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-safe", "0", "-f", "concat",
            "-i", str(lst), "-map", "0", "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(out),
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0 or not out.exists():
            raise HTTPException(500, err.decode(errors="replace")[-500:])

    @app.get("/api/cameras/{cam_id}/export")
    async def export(cam_id: str, start: float = Query(...), end: float = Query(...)):
        camera_or_404(cam_id)
        tmpdir = Path(tempfile.mkdtemp(prefix="export-"))
        try:
            await _build_clip(cam_id, start, end, tmpdir / "clip.mp4", tmpdir / "list.ffconcat")
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
        tmpdir = Path(tempfile.mkdtemp(prefix="save-"))
        try:
            await _build_clip(cam_id, start, end, part, tmpdir / "list.ffconcat")
            part.replace(saved)                 # atomic within saved/
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        finally:
            _rmtree(tmpdir)
        return {"saved": fname, "size": saved.stat().st_size}

    @app.get("/api/saved")
    async def list_saved():
        d = storage.saved_dir()
        import os as _os
        out = [{"name": e.name, "size": e.stat().st_size, "ctime": e.stat().st_mtime,
                "url": f"saved/{e.name}"} for e in _os.scandir(d) if e.is_file() and e.name.endswith(".mp4")]
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

    @app.delete("/api/saved/{name}")
    async def delete_saved(name: str):
        p = _safe_saved(name)
        if not p:
            raise HTTPException(404)
        p.unlink()
        return {"deleted": name}

    @app.get("/api/status")
    async def status():
        r = cfg.retention
        return {"storage": await asyncio.to_thread(storage.usage),
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
