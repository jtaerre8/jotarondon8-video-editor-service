import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse

app = FastAPI(title="jotarondon8-video-editor")

MAX_UPLOAD_BYTES = 300 * 1024 * 1024  # 300 MB
MAX_MUSIC_BYTES = 50 * 1024 * 1024  # 50 MB
TIMECODE_RE = re.compile(r"^\d{1,2}(:\d{2}){0,2}(\.\d+)?$")
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
PUBLIC_FILE_TTL_SECONDS = 30 * 60  # Instagram debe poder descargarlo antes de que se borre
WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL", "base")

WORKDIR = Path(tempfile.gettempdir()) / "video-editor-service"
PUBLIC_DIR = WORKDIR / "public"
PUBLIC_DIR.mkdir(parents=True, exist_ok=True)

_whisper_model = None


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


def save_upload(file: UploadFile, dest: Path, max_bytes: int = MAX_UPLOAD_BYTES) -> None:
    size = 0
    with dest.open("wb") as out:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise HTTPException(413, f"Archivo demasiado grande (máx {max_bytes // (1024*1024)}MB)")
            out.write(chunk)


def download_music(url: str, dest: Path) -> None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "jotarondon8-video-editor/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp, dest.open("wb") as out:
            size = 0
            while chunk := resp.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_MUSIC_BYTES:
                    raise HTTPException(413, "El archivo de música supera 50MB")
                out.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, f"No se pudo descargar la música: {exc}")


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


def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel

        _whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
    return _whisper_model


def format_srt_timestamp(t: float) -> str:
    t = max(0.0, t)
    hours = int(t // 3600)
    minutes = int((t % 3600) // 60)
    seconds = int(t % 60)
    millis = int(round((t - int(t)) * 1000))
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def build_subtitles(audio_path: Path, duration: int) -> str | None:
    try:
        model = get_whisper_model()
        segments, _ = model.transcribe(str(audio_path), vad_filter=True)
        lines = []
        idx = 1
        for seg in segments:
            start = max(0.0, seg.start)
            end = min(float(duration), seg.end)
            text = seg.text.strip()
            if not text or end <= start:
                continue
            lines.append(f"{idx}\n{format_srt_timestamp(start)} --> {format_srt_timestamp(end)}\n{text}\n")
            idx += 1
        return "\n".join(lines) if lines else None
    except Exception:
        return None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/reel", dependencies=[Depends(require_api_key)])
def reel(
    background_tasks: BackgroundTasks,
    file: UploadFile,
    overlay: UploadFile | None = None,
    start: str = Form("00:00:00"),
    duration: int = Form(60),
    music_url: str = Form(""),
):
    """Recorta un clip, lo convierte a vertical 9:16, le aplica color/zoom/fades,
    quema subtitulos automaticos y el pie de marca, y opcionalmente mezcla musica
    de fondo bajada de `music_url`."""
    if not TIMECODE_RE.match(start):
        raise HTTPException(400, "start inválido, usa formato HH:MM:SS")
    if not (1 <= duration <= 180):
        raise HTTPException(400, "duration debe estar entre 1 y 180 segundos")

    base_url = public_base_url()
    job_id = uuid.uuid4().hex
    in_path = WORKDIR / f"{job_id}-in.mp4"
    overlay_path = WORKDIR / f"{job_id}-overlay.png"
    music_path = WORKDIR / f"{job_id}-music"
    audio_path = WORKDIR / f"{job_id}-audio.wav"
    srt_path = WORKDIR / f"{job_id}-subs.srt"
    intermediate_path = WORKDIR / f"{job_id}-mid.mp4"
    out_path = PUBLIC_DIR / f"{job_id}.mp4"

    save_upload(file, in_path)
    has_overlay = overlay is not None
    if has_overlay:
        save_upload(overlay, overlay_path)
    has_music = bool(music_url.strip())
    if has_music:
        download_music(music_url.strip(), music_path)

    try:
        # 1) Extraer el audio ya recortado para transcribirlo (timestamps 0..duration).
        run_ffmpeg([
            "-ss", start, "-i", str(in_path), "-t", str(duration),
            "-vn", "-ac", "1", "-ar", "16000",
            str(audio_path),
        ])
        srt_text = build_subtitles(audio_path, duration)
        if srt_text:
            srt_path.write_text(srt_text, encoding="utf-8")

        # 2) Pase de video: vertical + color + zoom lento + fades (+ musica si hay).
        fade_out_start = max(duration - 0.6, 0)
        video_filter = (
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,gblur=sigma=30[bg];"
            "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2:format=auto[comp];"
            "[comp]eq=contrast=1.08:saturation=1.2:brightness=0.02,unsharp=5:5:0.4[graded];"
            "[graded]zoompan=z='min(zoom+0.0007,1.08)':d=1:s=1080x1920:fps=30[zoomed];"
            f"[zoomed]fade=t=in:st=0:d=0.5,fade=t=out:st={fade_out_start}:d=0.6[va]"
        )
        inputs = ["-ss", start, "-i", str(in_path)]
        if has_music:
            inputs += ["-stream_loop", "-1", "-i", str(music_path)]
            audio_filter = (
                "[0:a]volume=0.25[origA];"
                f"[1:a]afade=t=out:st={fade_out_start}:d=0.6,volume=1.0[musicA];"
                "[origA][musicA]amix=inputs=2:duration=first:dropout_transition=0[aout]"
            )
        else:
            audio_filter = f"[0:a]afade=t=in:st=0:d=0.3,afade=t=out:st={fade_out_start}:d=0.6[aout]"

        run_ffmpeg([
            *inputs,
            "-t", str(duration),
            "-filter_complex", f"{video_filter};{audio_filter}",
            "-map", "[va]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            str(intermediate_path),
        ])

        # 3) Pase final: quemar subtitulos (si hay) + pie de marca, sin re-codificar audio.
        video_parts = ["[0:v]"]
        chain = []
        if srt_text:
            style = "FontName=DejaVu Sans,FontSize=15,PrimaryColour=&H4CA2D4,BorderStyle=3,Outline=0,Shadow=0,MarginV=270"
            chain.append(f"subtitles='{srt_path}':force_style='{style}'")
        final_inputs = ["-i", str(intermediate_path)]
        if has_overlay:
            final_inputs += ["-i", str(overlay_path)]
            if chain:
                filt = f"[0:v]{chain[0]}[subbed];[subbed][1:v]overlay=0:main_h-overlay_h:format=auto,format=yuv420p[vout]"
            else:
                filt = "[0:v][1:v]overlay=0:main_h-overlay_h:format=auto,format=yuv420p[vout]"
        else:
            if chain:
                filt = f"[0:v]{chain[0]},format=yuv420p[vout]"
            else:
                filt = "[0:v]format=yuv420p[vout]"

        run_ffmpeg([
            *final_inputs,
            "-filter_complex", filt,
            "-map", "[vout]", "-map", "0:a",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(out_path),
        ])
    finally:
        for p in (in_path, overlay_path, music_path, audio_path, srt_path, intermediate_path):
            p.unlink(missing_ok=True)

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
