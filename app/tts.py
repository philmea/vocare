"""Text-to-speech providers. Each returns MP3 bytes for a chunk of text."""
from __future__ import annotations

import asyncio
import os
import re

import httpx


class TTSError(Exception):
    pass


class Provider:
    id = ""
    name = ""
    max_chars = 3000      # characters per request / per audio part
    parallel = 2          # concurrent requests per chapter
    voices: list[dict] = []   # [{"id":..., "name":...}]
    default_voice = ""

    def available(self) -> bool:
        return True

    async def synthesize(self, text: str, voice: str) -> bytes:  # pragma: no cover
        raise NotImplementedError


class EdgeProvider(Provider):
    """Microsoft Edge's online neural voices via the `edge-tts` package. Free, no key."""
    id = "edge"
    name = "Microsoft Edge neural voices (free)"
    max_chars = 4000
    parallel = 2
    voices = [{"id": v, "name": v.replace("Neural", "").replace("-", " ")} for v in [
        "en-US-AndrewMultilingualNeural", "en-US-BrianMultilingualNeural",
        "en-US-AvaMultilingualNeural", "en-US-EmmaMultilingualNeural",
        "en-US-ChristopherNeural", "en-US-GuyNeural", "en-US-EricNeural",
        "en-US-JennyNeural", "en-US-AriaNeural", "en-US-MichelleNeural",
        "en-GB-RyanNeural", "en-GB-ThomasNeural", "en-GB-SoniaNeural", "en-GB-LibbyNeural",
        "en-AU-WilliamNeural", "en-AU-NatashaNeural", "en-IE-ConnorNeural",
    ]]
    default_voice = "en-US-AndrewMultilingualNeural"

    async def synthesize(self, text: str, voice: str) -> bytes:
        import edge_tts

        communicate = edge_tts.Communicate(text, voice)
        audio = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio.extend(chunk["data"])
        if not audio:
            raise TTSError("Edge TTS returned no audio")
        return bytes(audio)


class OpenAIProvider(Provider):
    id = "openai"
    name = "OpenAI"
    max_chars = 3500   # API limit is 4096 characters per request
    parallel = 3
    voices = [{"id": v, "name": v.title()} for v in [
        "onyx", "ash", "ballad", "coral", "echo", "fable", "nova", "sage", "shimmer", "verse", "alloy",
    ]]
    default_voice = "onyx"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.key = os.environ.get("OPENAI_API_KEY", "")
        self.model = os.environ.get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
        self.instructions = os.environ.get(
            "OPENAI_TTS_INSTRUCTIONS",
            "You are narrating a fantasy audiobook. Read with a warm, engaging, expressive "
            "storytelling voice, giving dialogue natural emotion. Keep a steady pace.",
        )

    def available(self) -> bool:
        return bool(self.key)

    async def synthesize(self, text: str, voice: str) -> bytes:
        body = {"model": self.model, "voice": voice, "input": text, "response_format": "mp3"}
        if self.model.startswith("gpt-") and self.instructions:
            body["instructions"] = self.instructions
        r = await self.client.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {self.key}"}, json=body, timeout=180,
        )
        if r.status_code != 200:
            raise TTSError(f"OpenAI TTS error {r.status_code}: {r.text[:300]}")
        return r.content


class ElevenLabsProvider(Provider):
    id = "elevenlabs"
    name = "ElevenLabs"
    max_chars = 4000
    parallel = 2

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.key = os.environ.get("ELEVENLABS_API_KEY", "")
        self.model = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")
        # Format: "Name:voice_id,Other Name:voice_id"
        spec = os.environ.get("ELEVENLABS_VOICES", "George:JBFqnCBsd6RMkjVDRZzb")
        self.voices = []
        for item in spec.split(","):
            if ":" in item:
                name, vid = item.rsplit(":", 1)
                self.voices.append({"id": vid.strip(), "name": name.strip()})
        self.default_voice = self.voices[0]["id"] if self.voices else ""

    def available(self) -> bool:
        return bool(self.key and self.voices)

    async def synthesize(self, text: str, voice: str) -> bytes:
        r = await self.client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": self.key},
            json={"text": text, "model_id": self.model}, timeout=180,
        )
        if r.status_code != 200:
            raise TTSError(f"ElevenLabs error {r.status_code}: {r.text[:300]}")
        return r.content


def build_providers(client: httpx.AsyncClient) -> dict[str, Provider]:
    all_providers = [OpenAIProvider(client), ElevenLabsProvider(client), EdgeProvider()]
    return {p.id: p for p in all_providers if p.available()}


async def synthesize_with_retry(provider: Provider, text: str, voice: str, attempts: int = 4) -> bytes:
    delay = 2.0
    for i in range(attempts):
        try:
            return await provider.synthesize(text, voice)
        except Exception:
            if i == attempts - 1:
                raise
            await asyncio.sleep(delay)
            delay *= 2
    raise TTSError("unreachable")


_SENTENCE_RE = re.compile(r"(?<=[.!?…])[\"'”’)\]]*\s+")


def chunk_paragraphs(paragraphs: list[str], max_chars: int) -> list[str]:
    """Group paragraphs into chunks of at most max_chars, splitting long ones on sentences."""
    pieces: list[str] = []
    for para in paragraphs:
        if len(para) <= max_chars:
            pieces.append(para)
            continue
        current = ""
        for sentence in _SENTENCE_RE.split(para):
            while len(sentence) > max_chars:  # pathological: no sentence breaks
                cut = sentence.rfind(" ", 0, max_chars)
                cut = cut if cut > 0 else max_chars
                if current:
                    pieces.append(current)
                    current = ""
                pieces.append(sentence[:cut])
                sentence = sentence[cut:].lstrip()
            if current and len(current) + 1 + len(sentence) > max_chars:
                pieces.append(current)
                current = sentence
            else:
                current = f"{current} {sentence}".strip()
        if current:
            pieces.append(current)

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + 2 + len(piece) > max_chars:
            chunks.append(current)
            current = piece
        else:
            current = f"{current}\n\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks
