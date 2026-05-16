import base64
import io
import queue
import threading
import time
import wave
from config import settings

import numpy as np
import sounddevice as sd
from sarvamai import SarvamAI

RATE = 16000
CHUNK = 6400
MIN_AUDIO = 20
MAX_WAIT_SECONDS = 30


def _pcm_to_wav(samples: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(RATE)
        wf.writeframes(samples.tobytes())
    return buf.getvalue()


class Listener:
    def __init__(self):
        self.audio_queue: queue.Queue[np.ndarray] = queue.Queue()
        self._stop = threading.Event()

    def _callback(self, indata, frames, time_info, status):
        self.audio_queue.put(indata.copy())

    def listen(self) -> str | None:
        api_key = settings.sarvam_api_key
        if not api_key:
            return None

        self._stop.clear()
        transcript = ""
        utterance_final = False
        any_audio_sent = False
        start_time = time.time()

        client = SarvamAI(api_subscription_key=api_key)

        stream = sd.InputStream(
            samplerate=RATE,
            channels=1,
            dtype="int16",
            blocksize=CHUNK,
            callback=self._callback,
        )
        stream.start()

        with client.speech_to_text_streaming.connect(
            language_code=settings.voice_language,
            model="saaras:v3",
            mode="transcribe",
            high_vad_sensitivity="true",
            vad_signals="true",
        ) as ws:

            def drain():
                nonlocal transcript, utterance_final
                for resp in ws:
                    if resp.type == "data":
                        transcript = resp.data.transcript
                    elif resp.type == "events":
                        if hasattr(resp.data, "signal_type") and resp.data.signal_type == "END_SPEECH":
                            utterance_final = True
                    elif resp.type == "error":
                        break

            drain_thread = threading.Thread(target=drain, daemon=True)
            drain_thread.start()

            while not self._stop.is_set():
                if utterance_final and any_audio_sent:
                    ws.flush()
                    time.sleep(0.3)
                    break

                if time.time() - start_time > MAX_WAIT_SECONDS:
                    break

                try:
                    data = self.audio_queue.get(timeout=0.05)
                except queue.Empty:
                    continue

                samples: np.ndarray = data.flatten()
                max_val = float(np.max(np.abs(samples)))
                if max_val < MIN_AUDIO and not any_audio_sent:
                    continue

                wav_bytes = _pcm_to_wav(samples)
                any_audio_sent = True
                ws.transcribe(
                    audio=base64.b64encode(wav_bytes).decode(),
                    encoding="audio/wav",
                    sample_rate=RATE,
                )

        stream.stop()
        self._stop.set()
        return transcript.strip() or None
