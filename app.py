"""Vercel-ready YouTube audio converter and web server."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
WINDOWS_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", re.IGNORECASE)
ALLOWED_QUALITIES = {128, 192, 256}
MAX_OUTPUT_BYTES = 250 * 1024 * 1024
CONVERSION_TIMEOUT_SECONDS = 240
SLOTS = threading.BoundedSemaphore(value=2)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


class VideoRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class ConvertRequest(VideoRequest):
    quality: int = 192


def canonical_video_url(value: str) -> tuple[str, str]:
    """Accept video URLs only and discard untrusted query parameters."""
    try:
        parsed = urlsplit(value.strip())
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(422, "Enter a valid YouTube video link.") from exc

    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or port:
        raise HTTPException(422, "Enter a valid YouTube video link.")

    if hostname in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parsed.path == "/watch":
            candidates = parse_qs(parsed.query).get("v", [])
            video_id = candidates[0] if len(candidates) == 1 else ""
        else:
            path_parts = parsed.path.strip("/").split("/")
            video_id = path_parts[1] if len(path_parts) == 2 and path_parts[0] in {"shorts", "live"} else ""
    elif hostname == "youtu.be":
        video_id = parsed.path.strip("/")
    else:
        video_id = ""

    if not VIDEO_ID.fullmatch(video_id):
        raise HTTPException(422, "Enter a link to a single YouTube video or Short.")
    return f"https://www.youtube.com/watch?v={video_id}", video_id


def safe_filename(title: str) -> str:
    base = INVALID_FILENAME.sub("", title).strip(" .")
    base = re.sub(r"\s+", " ", base)[:100].rstrip(" .")
    if WINDOWS_RESERVED.fullmatch(base):
        base = f"audio-{base}"
    return f"{base or 'youtube-audio'}.mp3"


def inspect_video(url: str) -> dict[str, str | int | None]:
    canonical, video_id = canonical_video_url(url)
    command = [
        sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist", "--no-progress",
        "--no-warnings", "--socket-timeout", "15", "--retries", "1",
        "--js-runtimes", "node", "--dump-single-json", canonical,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=35, check=False)
    except FileNotFoundError as exc:
        raise HTTPException(503, "The converter is not installed on this server.") from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "YouTube took too long to respond. Please try again.") from exc

    if result.returncode != 0:
        raise HTTPException(422, "This video is unavailable here. Try a public video without an age or region restriction.")
    try:
        metadata = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(502, "The video details could not be read.") from exc

    if metadata.get("id") != video_id:
        raise HTTPException(422, "This link did not resolve to the requested video.")
    if metadata.get("is_live") or metadata.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
        raise HTTPException(422, "Live streams cannot be converted. Try again after the video is published.")

    title = str(metadata.get("title") or "YouTube audio").strip()[:200]
    duration = metadata.get("duration")
    return {
        "id": video_id,
        "title": title,
        "duration": round(duration) if isinstance(duration, (int, float)) and duration >= 0 else None,
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        "filename": safe_filename(title),
        "url": canonical,
    }


def _stop_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is not None and process.poll() is None:
        process.kill()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


def download_command(url: str) -> list[str]:
    return [
        sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist", "--no-progress",
        "--no-warnings", "--socket-timeout", "15", "--retries", "1",
        "--fragment-retries", "1", "--js-runtimes", "node",
        "--format", "bestaudio/best", "--output", "-", url,
    ]


def convert_video(url: str, quality: int, cancelled: threading.Event) -> tuple[tempfile.TemporaryDirectory[str], Path, dict]:
    if quality not in ALLOWED_QUALITIES:
        raise HTTPException(422, "Choose 128, 192, or 256 kbps.")
    video = inspect_video(url)
    if not SLOTS.acquire(blocking=False):
        raise HTTPException(429, "The converter is busy. Please try again shortly.")

    directory: tempfile.TemporaryDirectory[str] | None = None
    downloader: subprocess.Popen[bytes] | None = None
    encoder: subprocess.Popen[bytes] | None = None
    try:
        directory = tempfile.TemporaryDirectory(prefix="audio-convert-")
        target = Path(directory.name) / "audio.mp3"
        with open(Path(directory.name) / "download.log", "wb") as download_log, open(
            Path(directory.name) / "encode.log", "wb"
        ) as encode_log:
            downloader = subprocess.Popen(
                download_command(str(video["url"])),
                stdout=subprocess.PIPE,
                stderr=download_log,
            )
            assert downloader.stdout is not None
            encoder = subprocess.Popen(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                    "-i", "pipe:0", "-map", "0:a:0", "-vn", "-codec:a", "libmp3lame",
                    "-b:a", f"{quality}k", "-f", "mp3", str(target),
                ],
                stdin=downloader.stdout,
                stdout=subprocess.DEVNULL,
                stderr=encode_log,
            )
            downloader.stdout.close()
            started = time.monotonic()
            while encoder.poll() is None:
                if cancelled.is_set():
                    raise HTTPException(499, "The download was cancelled.")
                if time.monotonic() - started > CONVERSION_TIMEOUT_SECONDS:
                    raise HTTPException(504, "Conversion exceeded this server's time limit.")
                if target.exists() and target.stat().st_size > MAX_OUTPUT_BYTES:
                    raise HTTPException(413, "The MP3 exceeds this server's 250 MB limit.")
                time.sleep(0.2)

            try:
                downloader.wait(timeout=3)
            except subprocess.TimeoutExpired as exc:
                raise HTTPException(502, "The audio download did not finish.") from exc
            if downloader.returncode != 0 or encoder.returncode != 0 or not target.exists() or target.stat().st_size == 0:
                raise HTTPException(422, "Audio conversion failed. The video may be unavailable from this server.")
            if target.stat().st_size > MAX_OUTPUT_BYTES:
                raise HTTPException(413, "The MP3 exceeds this server's 250 MB limit.")
            return directory, target, video
    except BaseException as exc:
        _stop_process(encoder)
        _stop_process(downloader)
        if directory is not None:
            directory.cleanup()
        if isinstance(exc, FileNotFoundError):
            raise HTTPException(503, "The converter is not installed on this server.") from exc
        raise
    finally:
        SLOTS.release()


def _same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower():
        raise HTTPException(403, "Requests must come from this site.")


@app.get("/api/health")
def health() -> dict:
    ready = bool(importlib.util.find_spec("yt_dlp") and shutil.which("ffmpeg") and shutil.which("node"))
    return {"ready": ready}


@app.post("/api/inspect")
def inspect_endpoint(payload: VideoRequest, request: Request) -> dict:
    _same_origin(request)
    return inspect_video(payload.url)


@app.post("/api/convert")
async def convert_endpoint(payload: ConvertRequest, request: Request) -> StreamingResponse:
    _same_origin(request)
    cancelled = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(convert_video, payload.url, payload.quality, cancelled))
    while not task.done():
        if await request.is_disconnected():
            cancelled.set()
            await task
            raise HTTPException(499, "The download was cancelled.")
        await asyncio.sleep(0.2)
    directory, target, video = await task

    async def send_file():
        try:
            with target.open("rb") as source:
                while chunk := await asyncio.to_thread(source.read, 64 * 1024):
                    yield chunk
        finally:
            directory.cleanup()

    filename = str(video["filename"])
    return StreamingResponse(
        send_file(),
        media_type="audio/mpeg",
        headers={
            "Content-Disposition": f"attachment; filename=\"audio.mp3\"; filename*=UTF-8''{quote(filename)}",
            "Content-Length": str(target.stat().st_size),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html", media_type="text/html", headers={"Cache-Control": "no-store"})


@app.get("/styles.css")
def styles() -> FileResponse:
    return FileResponse(WEB / "styles.css", media_type="text/css")


@app.get("/app.js")
def script() -> FileResponse:
    return FileResponse(WEB / "app.js", media_type="application/javascript")


@app.get("/favicon.svg")
def favicon() -> FileResponse:
    return FileResponse(WEB / "favicon.svg", media_type="image/svg+xml")
