# Vocare

Paste a Royal Road fiction link and listen to it as an audiobook on your phone.

- Scrapes the chapter list and chapter text, with author notes and Royal Road's hidden anti-piracy lines removed. LitRPG stat tables are read out as short sentences.
- Sends the text to a TTS service. Each chapter is split into parts of a few minutes, so playback starts once the first part is ready while the rest generate.
- Caches every MP3 on the server. Each chapter is generated once per voice.
- Mobile web player:
  - lock-screen and headphone controls
  - 0.8–2.5× speed
  - −15s / +30s skip
  - sleep timer (minutes or end of chapter)
  - auto-advance, with the next chapter pre-generated
  - resumes where you left off
- Can be added to your home screen like an app.

## TTS options

| Service | Quality | Cost | Setup |
|---|---|---|---|
| **Microsoft Edge neural voices** | Good. "Andrew/Brian/Ava/Emma Multilingual" are the most natural | Free | None (always on) |
| **OpenAI** `gpt-4o-mini-tts` | Very good, expressive narration | Roughly $0.015 per audio minute, so about $0.30–0.50 for a typical 3–5k-word chapter | `OPENAI_API_KEY` |
| **ElevenLabs** | Best | Expensive for long serials (character-based plans) | `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICES` |

Start with Edge, which costs nothing. Use **Preview voice** in settings to compare.
The Edge voices go through an unofficial endpoint (the `edge-tts` package), so Microsoft could change or block it at any time.
Pricing changes too, so check the providers' pages.

## Run it

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env           # optional: add API keys / password
set -a; . ./.env; set +a
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `http://<computer-ip>:8000` on your phone (same Wi-Fi).

Or with Docker:

```bash
docker build -t vocare .
docker run -d --name vocare -p 8000:8000 --env-file .env -v vocare-data:/data vocare
```

### Listening away from home

The phone needs to reach the server. Two easy options:

1. **Home computer + [Tailscale](https://tailscale.com)** (free, private). Install Tailscale on the computer and your phone, then open `http://<computer-tailscale-name>:8000`. You don't need to expose anything to the internet.
2. **Cloud host** (Railway, Render, Fly.io, a small VPS) using the Dockerfile.
   - Attach a persistent volume at `/data` so cached audio survives restarts.
   - **Set `APP_PASSWORD`** so strangers can't spend your TTS credits.
   - The host's request timeout must allow ~2 minutes. The first audio request of a chapter waits while that part generates.

### Install on your phone

- **iPhone:** open the site in Safari → Share → *Add to Home Screen*.
- **Android:** open it in Chrome → ⋮ → *Add to Home screen* / *Install app*.

Audio keeps playing with the screen locked, and lock-screen controls skip and change chapters.

## Notes

- Progress and your library are stored in the phone's browser. Generated audio lives in `DATA_DIR` on the server. To free space, delete `data/audio/...`; it regenerates on demand.
- "Stubbed" fictions (like the Runeblade B2 example) only have the chapters the author left up, so that's all the app can play.
- Royal Road's HTML can change. If chapters come back empty or contain junk, check `GET /api/text/<fictionId>/<chapterId>` and adjust the selectors in `app/royalroad.py`.
- This is meant for personal listening. Please keep supporting authors (follows, ratings, Patreon). Don't redistribute generated audio.

## Tests

```bash
pip install pytest && python -m pytest -q tests
```
