"""Public terminal CLI API.

Keep this façade deliberately small so callers can continue to use
``from ares.cli import AresCLI`` while implementation modules stay organized.
"""

from .app import AresCLI
from .runtime import clear_current_task_cancellation as _clear_current_task_cancellation
from .runtime import history_path as _history_path

__all__ = ["AresCLI", "_history_path", "_clear_current_task_cancellation"]
