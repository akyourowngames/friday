from unittest.mock import MagicMock, patch
from ares.desktop.hotkey import HotkeyListener


def test_hotkey_listener_creation():
    listener = HotkeyListener(
        hotkey_ptt="ctrl+space",
        hotkey_mute="ctrl+shift+m",
        hotkey_window="ctrl+shift+h",
    )
    assert listener.ptt_callback is None
    assert listener.mute_callback is None
    assert listener.window_callback is None


def test_hotkey_listener_set_callbacks():
    listener = HotkeyListener(
        hotkey_ptt="ctrl+space",
        hotkey_mute="ctrl+shift+m",
        hotkey_window="ctrl+shift+h",
    )
    ptt_fn = MagicMock()
    mute_fn = MagicMock()
    window_fn = MagicMock()
    listener.set_callbacks(ptt=ptt_fn, mute=mute_fn, window=window_fn)
    assert listener.ptt_callback is ptt_fn
    assert listener.mute_callback is mute_fn
    assert listener.window_callback is window_fn


def test_hotkey_listener_parse_hotkey():
    keys = HotkeyListener._parse_hotkey("ctrl+shift+m")
    assert "ctrl" in keys
    assert "shift" in keys
    assert "m" in keys
