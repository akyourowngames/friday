import queue
from unittest.mock import patch

from ares.desktop.window import StatusWindow, StatusState


class _FakeEvent:
    def __init__(self):
        self.value = False

    def set(self):
        self.value = True

    def wait(self, timeout=None):
        return self.value


class _FakeQueue(queue.Queue):
    def cancel_join_thread(self):
        pass

    def close(self):
        pass

    def join_thread(self):
        pass


class _FakeProcess:
    def __init__(self, *, args, **kwargs):
        self.ready = args[2]
        self.alive = False

    def start(self):
        self.alive = True
        self.ready.set()

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        self.alive = False

    def terminate(self):
        self.alive = False

    def close(self):
        pass


class _FakeContext:
    Queue = _FakeQueue
    Event = _FakeEvent
    Process = _FakeProcess


def test_status_state_values():
    assert StatusState.IDLE == "idle"
    assert StatusState.AWAKE == "awake"
    assert StatusState.LISTENING == "listening"
    assert StatusState.THINKING == "thinking"
    assert StatusState.SPEAKING == "speaking"
    assert StatusState.MUTED == "muted"
    assert StatusState.ERROR == "error"


def test_status_window_creation():
    """StatusWindow controls an isolated process without creating Tk in tests."""
    with patch("ares.desktop.window.importlib.util.find_spec", return_value=object()):
        win = StatusWindow(opacity=0.85, _mp_context=_FakeContext())
        assert win.state == StatusState.IDLE
        win.set_state(StatusState.LISTENING)
        assert win.state == StatusState.LISTENING
        assert win.is_visible is True
        win.destroy()
        assert win.is_visible is False


def test_tool_result_extracts_file_paths_for_ui_cards():
    payload = '{"files":[{"path":"C:\\\\work\\\\notes.txt"},{"name":"README.md"}]}'
    items = __import__("ares.desktop.window", fromlist=["_StatusWindowUI"])._StatusWindowUI._result_items(payload)
    assert any("notes.txt" in item for item in items)
    assert "README.md" in items


def test_window_display_cleaner_removes_emoji_but_keeps_file_paths():
    ui = __import__("ares.desktop.window", fromlist=["_StatusWindowUI"])._StatusWindowUI
    cleaned = ui._clean_display_text("😊 C:\\work\\notes-old.txt")
    assert "😊" not in cleaned
    assert cleaned.strip() == "C:\\work\\notes-old.txt"
