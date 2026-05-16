import atexit
import os
import tempfile
import threading

import edge_tts
import pygame

_init_lock = threading.Lock()
_initialized = False
_temp_files: list[str] = []


def _ensure_mixer():
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        pygame.mixer.init()
        _initialized = True


@atexit.register
def _cleanup():
    for path in _temp_files:
        try:
            os.remove(path)
        except OSError:
            pass


def speak(text: str):
    if not text.strip():
        return
    _ensure_mixer()

    fd, path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    _temp_files.append(path)

    edge_tts.Communicate(text, "en-US-EmmaMultilingualNeural").save_sync(path)

    pygame.mixer.music.load(path)
    pygame.mixer.music.play()
    while pygame.mixer.music.get_busy():
        threading.Event().wait(0.1)
