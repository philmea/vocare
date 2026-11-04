import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"


def test_parse_fiction():
    from app import royalroad as rr
    f = rr.parse_fiction_page((FIX / "fiction.html").read_text(), 94966)
    assert f.title.startswith("Runeblade")
    assert f.author == "Some Author"
    assert [c.id for c in f.chapters] == [111, 222]
    assert f.chapters[1].url == "https://www.royalroad.com/fiction/94966/runeblade/chapter/222/two"
    assert rr.parse_fiction_id(
        "https://www.royalroad.com/fiction/94966/runeblade-a-delving?utm_source=home") == 94966


def test_parse_chapter_removes_hidden_and_theft_lines():
    from app import royalroad as rr
    ch = rr.parse_chapter_page((FIX / "chapter.html").read_text(), 111)
    assert ch.title == "Chapter 1 - Awakening"
    text = "\n".join(ch.paragraphs)
    assert "Unauthorized" not in text and "Amazon" not in text and "Patreon" not in text
    assert ch.paragraphs[0] == "Kael opened his eyes to darkness."
    assert '"Where am I?" he whispered.' in ch.paragraphs
    assert "Nobody answered." in ch.paragraphs
    assert "Name: Kael." in ch.paragraphs and "Strength. 10." in ch.paragraphs
    assert ch.paragraphs[-1] == "A final line."


def test_chunking():
    from app.tts import chunk_paragraphs
    paras = ["Short one."] * 50 + ["Long sentence here. " * 400]
    chunks = chunk_paragraphs(paras, 1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "".join(chunks).replace("\n", "").replace(" ", "") == "".join(paras).replace(" ", "")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    from app import main, royalroad as rr, tts
    importlib.reload(main)

    async def fake_fiction(client, fid):
        return rr.parse_fiction_page((FIX / "fiction.html").read_text(), fid)

    async def fake_chapter(client, chapter):
        return rr.parse_chapter_page((FIX / "chapter.html").read_text(), chapter.id)

    mp3 = tmp_path / "beep.mp3"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    str(mp3)], check=True)

    class Fake(tts.Provider):
        id, name, max_chars, parallel = "edge", "Fake", 40, 2
        voices = [{"id": "v1", "name": "V1"}]
        default_voice = "v1"
        calls = 0

        async def synthesize(self, text, voice):
            Fake.calls += 1
            await asyncio.sleep(0.05)
            return mp3.read_bytes()

    monkeypatch.setattr(rr, "get_fiction", fake_fiction)
    monkeypatch.setattr(rr, "get_chapter", fake_chapter)
    monkeypatch.setattr(tts, "build_providers", lambda c: {"edge": Fake()})
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        c.fake = Fake
        yield c


def test_end_to_end(client):
    cfg = client.get("/api/config").json()
    assert cfg["providers"][0]["id"] == "edge"
    f = client.get("/api/fiction", params={"url": "https://www.royalroad.com/fiction/94966/x"}).json()
    assert len(f["chapters"]) == 2
    q = {"provider": "edge", "voice": "v1"}
    r = client.get("/api/audio/94966/111/0.mp3", params=q)
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"
    # wait for the rest
    for _ in range(100):
        st = client.get("/api/status/111", params=q).json()
        if st["state"] == "done":
            break
        import time; time.sleep(0.05)
    assert st["state"] == "done" and st["total"] > 1 and st["ready"] == st["total"]
    calls = client.fake.calls
    # cached: no new synthesis
    assert client.post("/api/prepare/94966/111", params=q).json()["state"] == "done"
    assert client.fake.calls == calls
    assert client.get(f"/api/audio/94966/111/{st['total']}.mp3", params=q).status_code == 404
    assert client.get("/api/audio/94966/111/0.mp3", params={"provider": "edge", "voice": "nope"}).status_code == 400
    # Range requests (needed by iOS Safari)
    r = client.get("/api/audio/94966/111/0.mp3", params=q, headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100


def test_password(client, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "APP_PASSWORD", "hunter2")
    monkeypatch.setattr(main, "AUTH_TOKEN", "tok")
    assert client.get("/api/fiction", params={"url": "94966"}).status_code == 401
    assert client.post("/api/login", json={"password": "bad"}).status_code == 401
    assert client.post("/api/login", json={"password": "hunter2"}).status_code == 200
    assert client.get("/api/fiction", params={"url": "94966"}).status_code == 200
