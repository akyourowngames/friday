import numpy as np

from ares.voice.wakeword import OpenWakeWordDetector


class _FakeModel:
    def __init__(self, scores):
        self.scores = iter(scores)
        self.calls = []
        self.reset_count = 0

    def predict(self, audio):
        self.calls.append(audio)
        return {"hey_jarvis_v0.1": next(self.scores)}

    def reset(self):
        self.reset_count += 1


def _detector(scores, threshold=0.3):
    detector = object.__new__(OpenWakeWordDetector)
    detector.threshold = threshold
    detector.cooldown_seconds = 0.0
    detector.model_directory = None
    detector._audio_buffer = np.array([], dtype=np.float32)
    detector._last_activation = 0.0
    detector.last_score = 0.0
    detector._model = _FakeModel(scores)
    return detector


def test_streaming_detector_uses_80ms_pcm16_frames():
    detector = _detector([0.1, 0.8])

    assert detector.process(np.ones(480, dtype=np.float32) * 0.1) is False
    assert detector.process(np.ones(800, dtype=np.float32) * 0.1) is False
    assert detector.process(np.ones(1280, dtype=np.float32) * 0.1) is True

    assert len(detector._model.calls) == 2
    assert all(call.shape == (1280,) for call in detector._model.calls)
    assert all(call.dtype == np.int16 for call in detector._model.calls)


def test_detector_reset_clears_stream_state():
    detector = _detector([0.1])
    detector._audio_buffer = np.ones(500, dtype=np.float32)
    detector.last_score = 0.6

    detector.reset()

    assert detector._audio_buffer.size == 0
    assert detector.last_score == 0.0
    assert detector._model.reset_count == 1
