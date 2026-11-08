import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse

app = FastAPI(title="jotarondon8-video-editor")

MAX_UPLOAD_BYTES = 300 * 1024 * 1024  # 300 MB
TIMECODE_RE = re.compile(r"^\d{1,2}(:\d{2}){0,2}(\.\d+)?$")
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
PUBLIC_FILE_TTL_SECONDS = 30 * 60  # Instagram debe poder descargarlo antes de que se borre

WORKDIR = Path(tempfile.gettempdir()) / "video-editor-service"
PUBLIC_DIR = WORKDIR / "public"
PUBLIC_DIR.mkdir(parents=True, exist_ok=True)


def require_api_key(x_api_key: str = Header(default="")) -> None:
    expected = os.environ.get("API_KEY", "")
    if not expected:
        raise HTTPException(500, "API_KEY no configurada en el servicio")
    if not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(401, "API key inválida")


def public_base_url() -> str:
    url = os.environ.get("PUBLIC_BASE_URL", "")
    if not url:
        raise HTTPException(500, "PUBLIC_BASE_URL no configurada en el servicio")
    return url.rstrip("/")


def save_upload(file: UploadFile, dest: Path) -> None:
    size = 0
    with dest.open("wb") as out:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "Archivo demasiado grande (máx 300MB)")
            out.write(chunk)


def run_ffmpeg(args: list[str]) -> None:
    result = subprocess.run(
        ["ffmpeg", "-y", *args],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        raise HTTPException(422, f"ffmpeg falló: {result.stderr[-2000:]}")


def janitor() -> None:
    while True:
        cutoff = time.time() - PUBLIC_FILE_TTL_SECONDS
        for f in PUBLIC_DIR.glob("*.mp4"):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
        time.sleep(300)


@app.on_event("startup")
def start_janitor() -> None:
    threading.Thread(target=janitor, daemon=True).start()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/reel", dependencies=[Depends(require_api_key)])
def reel(
    file: UploadFile,
    overlay: UploadFile | None = None,
    start: str = Form("00:00:00"),
    duration: int = Form(60),
):
    """Recorta un clip, lo convierte a vertical 9:16 (fondo difuminado) y opcionalmente
    quema encima un PNG transparente (el pie de página de marca, renderizado en n8n)."""
    if not TIMECODE_RE.match(start):
        raise HTTPException(400, "start inválido, usa formato HH:MM:SS")
    if not (1 <= duration <= 180):
        raise HTTPException(400, "duration debe estar entre 1 y 180 segundos")

    base_url = public_base_url()
    job_id = uuid.uuid4().hex
    in_path = WORKDIR / f"{job_id}-in.mp4"
    overlay_path = WORKDIR / f"{job_id}-overlay.png"
    out_path = PUBLIC_DIR / f"{job_id}.mp4"
    save_upload(file, in_path)

    vertical = (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,gblur=sigma=30[bg];"
        "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2:format=auto[v]"
    )

    inputs = ["-ss", start, "-i", str(in_path)]
    if overlay is not None:
        save_upload(overlay, overlay_path)
        inputs += ["-i", str(overlay_path)]
        filter_complex = f"{vertical};[v][1:v]overlay=0:main_h-overlay_h:format=auto,format=yuv420p"
    else:
        filter_complex = f"{vertical.replace('[v]', '[tmp]')};[tmp]format=yuv420p"

    try:
        run_ffmpeg([
            *inputs,
            "-t", str(duration),
            "-filter_complex", filter_complex,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(out_path),
        ])
    finally:
        in_path.unlink(missing_ok=True)
        overlay_path.unlink(missing_ok=True)

    return {"job_id": job_id, "url": f"{base_url}/files/{job_id}.mp4"}


@app.get("/files/{job_id}.mp4")
def get_file(job_id: str):
    # Público a propósito: Instagram debe poder descargarlo sin header de API key.
    # job_id es un UUID impredecible y el archivo expira solo (ver `janitor`).
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(404)
    path = PUBLIC_DIR / f"{job_id}.mp4"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="video/mp4", filename="reel.mp4")


@app.delete("/files/{job_id}", dependencies=[Depends(require_api_key)])
def delete_file(job_id: str):
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(404)
    (PUBLIC_DIR / f"{job_id}.mp4").unlink(missing_ok=True)
    return {"deleted": job_id}
