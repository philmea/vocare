"""RR Audio: turn Royal Road chapters into audio you can play from your phone."""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import royalroad as rr
from . import tts

log = logging.getLogger("rr-audio")
logging.basicConfig(level=logging.INFO)

DATA_DIR = Path(os.environ.get("DATA_DIR", "data")).resolve()
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
MAX_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "2"))
FICTION_TTL = 15 * 60          # re-scrape the chapter list at most every 15 minutes
PART_WAIT_SECONDS = 150        # how long an audio request waits for its part to render
AUTH_COOKIE = "rr_auth"
AUTH_TOKEN = hmac.new(APP_PASSWORD.encode(), b"rr-audio-v1", hashlib.sha256).hexdigest() if APP_PASSWORD else ""
STATIC_DIR = Path(__file__).parent / "static"

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = httpx.AsyncClient()
    state["client"] = client
    state["providers"] = tts.build_providers(client)
    state["job_sem"] = asyncio.Semaphore(MAX_JOBS)
    state["jobs"] = {}
    state["fictions"] = {}
    for sub in ("fictions", "text", "audio"):
        (DATA_DIR / sub).mkdir(parents=True, exist_ok=True)
    log.info("Providers enabled: %s", ", ".join(state["providers"]))
    yield
    await client.aclose()


app = FastAPI(title="RR Audio", lifespan=lifespan)


# ---------------------------------------------------------------- auth

@app.middleware("http")
async def require_password(request: Request, call_next):
    path = request.url.path
    if APP_PASSWORD and path.startswith("/api/") and path not in ("/api/login", "/api/config"):
        if not hmac.compare_digest(request.cookies.get(AUTH_COOKIE, ""), AUTH_TOKEN):
            return JSONResponse({"detail": "Password required"}, status_code=401)
    return await call_next(request)


class LoginBody(BaseModel):
    password: str


@app.post("/api/login")
async def login(body: LoginBody):
    if not APP_PASSWORD:
        return {"ok": True}
    if not hmac.compare_digest(body.password, APP_PASSWORD):
        await asyncio.sleep(1)
        raise HTTPException(401, "Wrong password")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(AUTH_COOKIE, AUTH_TOKEN, max_age=365 * 86400, httponly=True, samesite="lax")
    return resp


@app.get("/api/config")
async def config(request: Request):
    providers = state["providers"]
    authed = not APP_PASSWORD or hmac.compare_digest(request.cookies.get(AUTH_COOKIE, ""), AUTH_TOKEN)
    return {
        "auth_required": bool(APP_PASSWORD),
        "authed": authed,
        "default_provider": next(iter(providers), None),
        "providers": [
            {"id": p.id, "name": p.name, "voices": p.voices, "default_voice": p.default_voice}
            for p in providers.values()
        ],
    }


# ---------------------------------------------------------------- Royal Road data

async def load_fiction(fiction_id: int, refresh: bool = False) -> rr.Fiction:
    cache = state["fictions"]
    path = DATA_DIR / "fictions" / f"{fiction_id}.json"
    hit = cache.get(fiction_id)
    if hit and not refresh and time.time() - hit[0] < FICTION_TTL:
        return hit[1]
    try:
        fiction = await rr.get_fiction(state["client"], fiction_id)
    except rr.RoyalRoadError:
        if path.exists() and not refresh:  # Royal Road down? serve the last copy we saw.
            data = json.loads(path.read_text())
            data["chapters"] = [rr.Chapter(**c) for c in data["chapters"]]
            return rr.Fiction(**data)
        raise
    path.write_text(json.dumps(dataclasses.asdict(fiction)))
    cache[fiction_id] = (time.time(), fiction)
    return fiction


async def load_chapter_text(fiction_id: int, chapter_id: int) -> rr.ChapterText:
    path = DATA_DIR / "text" / f"{chapter_id}.json"
    if path.exists():
        return rr.ChapterText(**json.loads(path.read_text()))
    fiction = await load_fiction(fiction_id)
    chapter = next((c for c in fiction.chapters if c.id == chapter_id), None)
    if chapter is None:  # maybe a brand-new chapter; refresh the list once
        fiction = await load_fiction(fiction_id, refresh=True)
        chapter = next((c for c in fiction.chapters if c.id == chapter_id), None)
    if chapter is None:
        raise rr.RoyalRoadError("Chapter not found in this fiction.")
    text = await rr.get_chapter(state["client"], chapter)
    path.write_text(json.dumps(dataclasses.asdict(text)))
    return text


@app.get("/api/fiction")
async def get_fiction(url: str, refresh: bool = False):
    try:
        fiction_id = rr.parse_fiction_id(url)
        fiction = await load_fiction(fiction_id, refresh=refresh)
    except rr.RoyalRoadError as e:
        raise HTTPException(400, str(e))
    return dataclasses.asdict(fiction)


@app.get("/api/text/{fiction_id}/{chapter_id}")
async def get_text(fiction_id: int, chapter_id: int):
    try:
        return dataclasses.asdict(await load_chapter_text(fiction_id, chapter_id))
    except rr.RoyalRoadError as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------- audio generation

_VOICE_RE = re.compile(r"^[\w.-]{1,80}$")


def check_voice(provider_id: str, voice: str) -> tts.Provider:
    provider = state["providers"].get(provider_id)
    if provider is None:
        raise HTTPException(400, f"TTS provider '{provider_id}' is not configured on the server.")
    if not _VOICE_RE.match(voice) or voice not in {v["id"] for v in provider.voices}:
        raise HTTPException(400, "Unknown voice.")
    return provider


def audio_dir(provider_id: str, voice: str, chapter_id: int) -> Path:
    return DATA_DIR / "audio" / provider_id / voice / str(chapter_id)


def part_path(d: Path, part: int) -> Path:
    return d / f"{part:03d}.mp3"


class Job:
    def __init__(self, fiction_id: int, chapter_id: int, provider: tts.Provider, voice: str):
        self.fiction_id, self.chapter_id, self.provider, self.voice = fiction_id, chapter_id, provider, voice
        self.dir = audio_dir(provider.id, voice, chapter_id)
        self.state = "queued"
        self.total: int | None = None
        self.error: str | None = None
        self.task: asyncio.Task | None = None

    def meta_path(self) -> Path:
        return self.dir / "meta.json"

    def write_meta(self):
        self.meta_path().write_text(json.dumps({"total": self.total, "state": self.state}))

    async def run(self):
        async with state["job_sem"]:
            try:
                self.state = "fetching"
                text = await load_chapter_text(self.fiction_id, self.chapter_id)
                chunks = tts.chunk_paragraphs([text.title + "."] + text.paragraphs, self.provider.max_chars)
                self.dir.mkdir(parents=True, exist_ok=True)
                self.total = len(chunks)
                self.state = "generating"
                self.write_meta()
                sem = asyncio.Semaphore(self.provider.parallel)

                async def render(i: int, chunk: str):
                    out = part_path(self.dir, i)
                    if out.exists():
                        return
                    async with sem:
                        audio = await tts.synthesize_with_retry(self.provider, chunk, self.voice)
                    tmp = out.with_suffix(".tmp")
                    tmp.write_bytes(audio)
                    tmp.replace(out)

                await asyncio.gather(*(render(i, c) for i, c in enumerate(chunks)))
                self.state = "done"
                self.write_meta()
                log.info("Chapter %s done (%d parts, %s/%s)", self.chapter_id, self.total,
                         self.provider.id, self.voice)
            except Exception as e:  # noqa: BLE001 - surface any failure to the client
                log.exception("Job failed for chapter %s", self.chapter_id)
                self.state = "error"
                self.error = str(e) or e.__class__.__name__


def status_of(job: Job | None, d: Path) -> dict:
    meta = json.loads((d / "meta.json").read_text()) if (d / "meta.json").exists() else {}
    total = job.total if job and job.total is not None else meta.get("total")
    ready = 0
    while total is not None and ready < total and part_path(d, ready).exists():
        ready += 1
    if total is not None and ready == total:
        st = "done"
    elif job:
        st = job.state
    else:
        st = "idle"
    return {"state": st, "total": total, "ready": ready, "error": job.error if job else None}


def ensure_job(fiction_id: int, chapter_id: int, provider: tts.Provider, voice: str) -> Job | None:
    key = (provider.id, voice, chapter_id)
    jobs: dict = state["jobs"]
    job = jobs.get(key)
    d = audio_dir(provider.id, voice, chapter_id)
    if job and (job.state == "error" or (job.task and job.task.done() and status_of(None, d)["state"] != "done")):
        job = None  # retry failed jobs, and regenerate if cached files were deleted
    if job is None:
        if status_of(None, d)["state"] == "done":
            return None
        job = Job(fiction_id, chapter_id, provider, voice)
        job.task = asyncio.create_task(job.run())
        jobs[key] = job
    return job


@app.post("/api/prepare/{fiction_id}/{chapter_id}")
async def prepare(fiction_id: int, chapter_id: int, provider: str = Query(...), voice: str = Query(...)):
    p = check_voice(provider, voice)
    job = ensure_job(fiction_id, chapter_id, p, voice)
    return status_of(job, audio_dir(provider, voice, chapter_id))


@app.get("/api/status/{chapter_id}")
async def status(chapter_id: int, provider: str = Query(...), voice: str = Query(...)):
    check_voice(provider, voice)
    job = state["jobs"].get((provider, voice, chapter_id))
    return status_of(job, audio_dir(provider, voice, chapter_id))


@app.get("/api/audio/{fiction_id}/{chapter_id}/{part}.mp3")
async def audio(fiction_id: int, chapter_id: int, part: int,
                provider: str = Query(...), voice: str = Query(...)):
    """Serve one audio part, starting generation and waiting for it if needed."""
    p = check_voice(provider, voice)
    d = audio_dir(provider, voice, chapter_id)
    path = part_path(d, part)
    deadline = time.monotonic() + PART_WAIT_SECONDS
    while not path.exists():
        job = ensure_job(fiction_id, chapter_id, p, voice)
        st = status_of(job, d)
        if st["state"] == "error":
            raise HTTPException(502, st["error"] or "Generation failed")
        if st["total"] is not None and part >= st["total"]:
            raise HTTPException(404, "No such part")
        if time.monotonic() > deadline:
            raise HTTPException(504, "Still generating, try again")
        await asyncio.sleep(0.5)
    return FileResponse(path, media_type="audio/mpeg",
                        headers={"Cache-Control": "private, max-age=31536000, immutable"})


@app.get("/api/preview")
async def preview(provider: str = Query(...), voice: str = Query(...)):
    p = check_voice(provider, voice)
    path = DATA_DIR / "audio" / provider / voice / "preview.mp3"
    if not path.exists():
        sample = ("The rune flared to life beneath her fingers. \"Well,\" she said, grinning, "
                  "\"that's new.\" Somewhere deeper in the dungeon, something answered.")
        try:
            audio_bytes = await tts.synthesize_with_retry(p, sample, voice, attempts=2)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"Preview failed: {e}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(audio_bytes)
    return FileResponse(path, media_type="audio/mpeg")


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
