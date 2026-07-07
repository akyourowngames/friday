"""Speaker diagnostic test — run this to check if TTS audio plays."""

import asyncio
import sys
import os

# Add project to path
sys.path.insert(0, os.path.dirname(__file__))


async def test_edge_tts():
    """Test 1: Generate TTS audio with Edge TTS."""
    print("=== Test 1: Edge TTS audio generation ===")
    try:
        from ares.voice.tts import EdgeTTS

        tts = EdgeTTS(voice="en-US-GuyNeural")
        print(f"  Provider: Edge TTS")
        print(f"  Voice: en-US-GuyNeural")
        print(f"  Generating audio for 'Hello, this is a test'...")

        audio = await tts.speak("Hello, this is a test of the speaker system.")
        print(f"  Audio size: {len(audio)} bytes")

        if len(audio) < 100:
            print("  [FAIL] Audio too small, likely empty")
            return None

        print("  [OK] Audio generated successfully")
        return audio

    except Exception as e:
        print(f"  [FAIL] {e}")
        return None


async def test_speak_stream():
    """Test 2: Test streaming TTS."""
    print("\n=== Test 2: Edge TTS streaming ===")
    try:
        from ares.voice.tts import EdgeTTS

        tts = EdgeTTS(voice="en-US-GuyNeural")
        chunks = []
        total = 0

        print("  Streaming audio...")
        async for chunk in tts.speak_stream("Streaming test. This should play as it arrives."):
            chunks.append(chunk)
            total += len(chunk)
            print(f"    Chunk {len(chunks)}: {len(chunk)} bytes (total: {total})")

        print(f"  Total chunks: {len(chunks)}, Total size: {total} bytes")
        if total < 100:
            print("  [FAIL] No audio data received")
            return None
        print("  [OK] Streaming works")
        return b"".join(chunks)

    except Exception as e:
        print(f"  [FAIL] {e}")
        return None


def test_play_audio_bytes(audio: bytes):
    """Test 3: Play audio using play_audio_bytes (blocking)."""
    print("\n=== Test 3: Play audio (blocking mode) ===")
    try:
        from ares.voice.player import play_audio_bytes
        import asyncio

        print(f"  Audio size: {len(audio)} bytes")
        print("  Playing (blocking)...")

        asyncio.run(play_audio_bytes(audio, speed=1.0))
        print("  [OK] Playback completed")

    except Exception as e:
        print(f"  [FAIL] {e}")


async def test_play_audio_stream(audio: bytes):
    """Test 4: Play audio using play_audio_stream (streaming mode)."""
    print("\n=== Test 4: Play audio (streaming mode) ===")
    try:
        from ares.voice.player import play_audio_stream, audio_bytes_to_pcm16
        import asyncio
        import numpy as np

        print(f"  Audio size: {len(audio)} bytes")

        # Convert to PCM16
        print("  Converting to PCM16...")
        pcm = audio_bytes_to_pcm16(audio, sample_rate=24000, speed=1.0)
        print(f"  PCM16 size: {len(pcm)} bytes ({len(pcm)//2} samples)")

        if len(pcm) < 100:
            print("  [FAIL] PCM16 data too small")
            return

        # Put in queue and play
        audio_q = asyncio.Queue()
        stop_event = asyncio.Event()

        await audio_q.put(pcm)
        await audio_q.put(None)

        print("  Playing (streaming)...")
        await play_audio_stream(audio_q, stop_event, sample_rate=24000)
        print("  [OK] Streaming playback completed")

    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()


def test_sounddevice_devices():
    """Test 5: Check audio output devices."""
    print("\n=== Test 5: Audio output devices ===")
    try:
        import sounddevice as sd

        devices = sd.query_devices()
        print("  Available devices:")
        for i, dev in enumerate(devices):
            marker = " <--" if dev['name'] == sd.default.device[1] else ""
            if dev['max_output_channels'] > 0:
                print(f"    [{i}] {dev['name']} (outputs: {dev['max_output_channels']}, rate: {dev['default_samplerate']}){marker}")

        default_out = sd.default.device[1]
        print(f"\n  Default output device: {default_out}")
        if default_out is not None:
            dev_info = sd.query_devices(default_out)
            print(f"  Device name: {dev_info['name']}")

    except Exception as e:
        print(f"  [FAIL] {e}")


def test_sounddevice_tone():
    """Test 6: Generate and play a simple tone."""
    print("\n=== Test 6: Play a test tone ===")
    try:
        import sounddevice as sd
        import numpy as np

        sr = 24000
        duration = 1.0
        freq = 440.0

        t = np.linspace(0, duration, int(sr * duration), endpoint=False)
        tone = (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)

        print(f"  Playing {freq}Hz tone for {duration}s...")
        sd.play(tone, sr)
        sd.wait()
        print("  [OK] Tone played")

    except Exception as e:
        print(f"  [FAIL] {e}")


async def main():
    print("=" * 60)
    print("  ARES SPEAKER DIAGNOSTIC")
    print("=" * 60)

    # Test output devices
    test_sounddevice_devices()

    # Test simple tone
    test_sounddevice_tone()

    # Test TTS generation
    audio = await test_edge_tts()
    if audio:
        # Test streaming generation
        await test_speak_stream()

        # Test blocking playback
        test_play_audio_bytes(audio)

        # Test streaming playback
        await test_play_audio_stream(audio)

    print("\n" + "=" * 60)
    print("  DIAGNOSTIC COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
