"""
End-to-end voice loop test — no human mic needed.

Joins the local LiveKit room as a participant, listens for Elfie's
greeting, then "speaks" to her by synthesizing a question with Cartesia
and streaming it in as mic audio. Watch the agent worker logs for
[STT] / [LLM] lines to confirm the full loop.

Run:  .venv/bin/python test_voice_loop.py
"""
import asyncio
import datetime

import httpx
from livekit import api, rtc

from elfie.config import CONFIG

SAMPLE_RATE = 48000
QUESTION = "Hey Elfie, what time is it right now?"


async def synthesize(text: str) -> bytes:
    """Get raw PCM for the test question from Cartesia."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            "https://api.cartesia.ai/tts/bytes",
            headers={
                "X-API-Key": CONFIG.cartesia_api_key,
                "Cartesia-Version": "2024-06-10",
            },
            json={
                "model_id": "sonic-2",
                "transcript": text,
                "voice": {"mode": "id", "id": CONFIG.voice.tts_voice},
                "output_format": {
                    "container": "raw",
                    "encoding": "pcm_s16le",
                    "sample_rate": SAMPLE_RATE,
                },
            },
        )
        r.raise_for_status()
        return r.content


async def main() -> None:
    token = (
        api.AccessToken(CONFIG.livekit_api_key, CONFIG.livekit_api_secret)
        .with_identity("test-user")
        .with_name("Test User")
        .with_grants(api.VideoGrants(room_join=True, room="elfie-test"))
        .with_ttl(datetime.timedelta(hours=1))
        .to_jwt()
    )

    room = rtc.Room()
    agent_frames = 0
    agent_spoke = asyncio.Event()

    @room.on("track_subscribed")
    def on_track(track, pub, participant):
        print(f"<< agent published audio track ({participant.identity})")

        async def listen():
            nonlocal agent_frames
            async for ev in rtc.AudioStream(track):
                # Count non-silent frames as evidence Elfie is speaking
                if max(abs(s) for s in ev.frame.data) > 500:  # data is int16 samples
                    agent_frames += 1
                    if agent_frames > 20:  # ~0.2s of real speech
                        agent_spoke.set()

        asyncio.ensure_future(listen())

    await room.connect(CONFIG.livekit_url, token)
    print(f">> joined room {room.name!r} as test-user")

    # Publish a mic track so the agent has an audio input
    source = rtc.AudioSource(SAMPLE_RATE, 1)
    track = rtc.LocalAudioTrack.create_audio_track("mic", source)
    # Must be tagged as a microphone — the agent only listens to mic tracks
    options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    await room.local_participant.publish_track(track, options)
    print(">> published mic track (silent for now)")

    # Wait for Elfie's greeting (first run also downloads the Silero model)
    print(">> waiting for Elfie to greet (up to 90s)...")
    try:
        await asyncio.wait_for(agent_spoke.wait(), timeout=90)
        print("<< GREETING HEARD — Elfie is speaking ✓")
    except asyncio.TimeoutError:
        print("!! no greeting heard within 90s — check worker logs")
        await room.disconnect()
        return

    # Ask quickly — before the 8s silence monitor prompts "Still there?"
    print(f">> asking: {QUESTION!r}")
    pcm = await synthesize(QUESTION)
    import array
    samples = array.array("h", pcm)
    dur = len(samples) / SAMPLE_RATE
    print(f">> synthesized {dur:.1f}s of audio, peak amplitude {max(abs(s) for s in samples)}")

    samples_per_chunk = SAMPLE_RATE // 100  # 10ms
    chunk_bytes = samples_per_chunk * 2

    async def push(data: bytes) -> None:
        for i in range(0, len(data) - chunk_bytes, chunk_bytes):
            await source.capture_frame(rtc.AudioFrame(
                data=data[i : i + chunk_bytes],
                sample_rate=SAMPLE_RATE,
                num_channels=1,
                samples_per_channel=samples_per_chunk,
            ))

    await push(bytes(SAMPLE_RATE))           # 0.5s lead-in silence (VAD onset)
    await push(pcm)                          # the question
    await push(bytes(SAMPLE_RATE * 4))       # 2s trailing silence (endpointing)
    print(">> question sent; observing for 30s (check worker log for [STT]/[LLM])...")

    await asyncio.sleep(30)
    await room.disconnect()
    print(">> disconnected")


if __name__ == "__main__":
    asyncio.run(main())
