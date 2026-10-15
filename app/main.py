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
                "live": f"live/{c.id}/index.m3u8",
            })
        return out

    @app.get("/api/cameras/{cam_id}/segments")
    async def segments(cam_id: str, start: float = Query(...), end: float = Query(...)):
        camera_or_404(cam_id)
        segs = await asyncio.to_thread(storage.segments, cam_id, start, end)
        return [{"name": s.path.name, "start": s.start, "end": s.end, "size": s.size,
                 "url": f"recordings/{cam_id}/{s.path.name}"} for s in segs]

    @app.get("/api/cameras/{cam_id}/events")
    async def events(cam_id: str, start: float = Query(...), end: float = Query(...)):
        camera_or_404(cam_id)
        store = sup.events
        if store is None:
            from .events import EventStore
            if not cfg.events_db.exists():
                return []
            store = EventStore(cfg.events_db)
        return await asyncio.to_thread(store.query, cam_id, start, end)

    @app.get("/api/cameras/{cam_id}/playback.mp4")
    async def playback(cam_id: str, start: float = Query(...), dur: float = Query(300)):
        """Transcode a window [start, start+dur) to H.264 and stream it progressively
        (fragmented MP4), so HEVC recordings are playable in any browser."""
        camera_or_404(cam_id)
        dur = min(max(dur, 1), 1800)
        script = storage.concat_list(cam_id, start, start + dur)
        if script.count("\nfile ") == 0:
            raise HTTPException(404, "no recordings at that time")
        tmpdir = Path(tempfile.mkdtemp(prefix="pb-"))
        (tmpdir / "list.ffconcat").write_text(script)
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-safe", "0",
            "-f", "concat", "-i", str(tmpdir / "list.ffconcat"), "-t", str(dur),
            "-vf", "scale=-2:720", "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
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
        camera_or_404(cam_id)
        from .recorder import thumbnail
        path, offset = await asyncio.to_thread(storage.segment_at, cam_id, ts)
        if not path:
            raise HTTPException(404, "no recording at that time")
        try:
            img = await thumbnail(path, offset)
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
