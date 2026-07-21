from unittest.mock import MagicMock, patch
from ares.desktop.window import StatusWindow, StatusState


def test_status_state_values():
    assert StatusState.IDLE == "idle"
    assert StatusState.LISTENING == "listening"
    assert StatusState.THINKING == "thinking"
    assert StatusState.SPEAKING == "speaking"
    assert StatusState.MUTED == "muted"
    assert StatusState.ERROR == "error"


def test_status_window_creation():
    """StatusWindow can be instantiated without starting the GUI loop."""
    mock_ctk = MagicMock()
    mock_root = MagicMock()
    mock_ctk.CTkToplevel.return_value = mock_root
    mock_ctk.CTkLabel.return_value = MagicMock()

    with patch("ares.desktop.window.customtkinter", mock_ctk, create=True):
        import sys
        # Patch the module-level import
        import ares.desktop.window as win_mod
        original_ctk = getattr(win_mod, "customtkinter", None)
        win_mod.customtkinter = mock_ctk
        try:
            # Need to patch it before instantiation
            with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
                win = StatusWindow(opacity=0.85)
                assert win.state == StatusState.IDLE
                win.destroy()
        finally:
            if original_ctk is not None:
                win_mod.customtkinter = original_ctk
