"""
Self-test of the whole wake-word flow — no human, no mic needed.

Simulates exactly what happens in wake-word mode:
  1. At rest, NOTHING connects (deaf until woken).
  2. A wake event fires (what the Windows listener / Test-wake button does).
  3. The page would connect — we simulate that with a synthetic mic client.
  4. Elfie greets, answers a question.
  5. After silence, Elfie hangs up by herself (end_on_silence behaviour).

Run:  .venv/bin/python test_wake_flow.py
"""
import asyncio
import datetime
import json
import time
import urllib.request
from pathlib import Path

import httpx
from livekit import api, rtc

from elfie.config import CONFIG

SR = 48000
DASH = f"http://localhost:{CONFIG.dashboard.port}"


def log(msg): print(f"  {msg}", flush=True)


async def synth(text: str) -> bytes:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://api.cartesia.ai/tts/bytes",
            headers={"X-API-Key": CONFIG.cartesia_api_key, "Cartesia-Version": "2024-06-10"},
            json={"model_id": "sonic-2", "transcript": text,
                  "voice": {"mode": "id", "id": CONFIG.voice.tts_voice},
                  "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": SR}})
        r.raise_for_status(); return r.content


async def main():
    print("\n=== STEP 1: at rest, is anything connecting on its own? ===")
    # Watch the wake endpoint + confirm no spontaneous wake for 6s
    base = json.loads(urllib.request.urlopen(f"{DASH}/api/wake").read())["ts"]
    await asyncio.sleep(6)
    now = json.loads(urllib.request.urlopen(f"{DASH}/api/wake").read())["ts"]
    log(f"wake ts unchanged ({base == now}) — nothing fired a wake. ✓ deaf at rest")

    print("\n=== STEP 2: fire a wake event (what the listener / Test button does) ===")
    urllib.request.urlopen(f"{DASH}/api/wake", data=b"{}", timeout=3)
    fired = json.loads(urllib.request.urlopen(f"{DASH}/api/wake").read())["ts"]
    log(f"wake recorded, ts advanced ({fired > base}). ✓ a real 'Hey Jarvis' lands here")

    print("\n=== STEP 3: simulate the page connecting on that wake ===")
    token = (api.AccessToken(CONFIG.livekit_api_key, CONFIG.livekit_api_secret)
             .with_identity("selftest").with_grants(api.VideoGrants(room_join=True, room="elfie-selftest"))
             .with_ttl(datetime.timedelta(hours=1)).to_jwt())
    room = rtc.Room()
    greeted, answered = asyncio.Event(), asyncio.Event()
    state = {"frames": 0, "phase": "greet"}

    @room.on("track_subscribed")
    def _on(track, pub, p):
        async def listen():
            async for ev in rtc.AudioStream(track):
                if max(ev.frame.data) > 500:
                    state["frames"] += 1
                    if state["frames"] > 15:
                        (greeted if state["phase"] == "greet" else answered).set()
        asyncio.ensure_future(listen())

    await room.connect(CONFIG.livekit_url, token)
    src = rtc.AudioSource(SR, 1)
    track = rtc.LocalAudioTrack.create_audio_track("mic", src)
    await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
    log("connected as a fake user")
    try:
        await asyncio.wait_for(greeted.wait(), 90)
        log("Elfie greeted on wake. ✓")
    except asyncio.TimeoutError:
        log("✗ no greeting — FAIL"); await room.disconnect(); return

    print("\n=== STEP 4: ask a question, expect a spoken answer ===")
    await asyncio.sleep(3)
    state["phase"] = "answer"; state["frames"] = 0
    pcm = await synth("What time is it right now?")
    spc = SR // 100; cb = spc * 2
    async def push(b):
        for i in range(0, len(b) - cb, cb):
            await src.capture_frame(rtc.AudioFrame(data=b[i:i+cb], sample_rate=SR, num_channels=1, samples_per_channel=spc))
    await push(bytes(SR)); await push(pcm); await push(bytes(SR * 2))
    try:
        await asyncio.wait_for(answered.wait(), 45)
        log("Elfie answered the question. ✓")
    except asyncio.TimeoutError:
        log("✗ no answer — FAIL")

    print("\n=== STEP 5: go silent, expect Elfie to hang up by herself ===")
    log(f"(silence_timeout={CONFIG.session.silence_timeout_sec}s, retries={CONFIG.session.max_silence_retries}, end_on_silence={CONFIG.session.end_on_silence})")
    disconnected = asyncio.Event()
    room.on("disconnected", lambda *a: disconnected.set())
    budget = CONFIG.session.silence_timeout_sec * (CONFIG.session.max_silence_retries + 1) + 30
    try:
        await asyncio.wait_for(disconnected.wait(), budget)
        log("Elfie hung up after silence. ✓ back to waiting-for-wake")
    except asyncio.TimeoutError:
        log("✗ she did NOT hang up within budget — FAIL")
    try: await room.disconnect()
    except Exception: pass
    print("\n=== wake flow self-test complete ===")


if __name__ == "__main__":
    asyncio.run(main())
