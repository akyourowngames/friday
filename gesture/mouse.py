import ctypes
import time

import numpy as np
import pyautogui

from .config import PINCH_DISTANCE, CURSOR_DEADZONE, CURSOR_VELOCITY_SLOW, CURSOR_VELOCITY_FAST, CURSOR_VELOCITY_THRESHOLD

SCREEN_W, SCREEN_H = pyautogui.size()
_LAST_X, _LAST_Y = pyautogui.position()
_MOUSE_DOWN = False
_LAST_CLICK_TIME = 0.0
_LAST_FRAME_TIME = time.time()

pyautogui.FAILSAFE = False
pyautogui.MINIMUM_DURATION = 0

_user32 = ctypes.windll.user32


def process_mouse(landmarks, classifier_gesture: str):
    global _LAST_X, _LAST_Y, _MOUSE_DOWN, _LAST_CLICK_TIME, _LAST_FRAME_TIME

    index_lm = landmarks[8]
    target_x = index_lm.x * SCREEN_W
    target_y = index_lm.y * SCREEN_H

    now = time.time()
    dt = max(now - _LAST_FRAME_TIME, 0.001)
    _LAST_FRAME_TIME = now

    dx = target_x - _LAST_X
    dy = target_y - _LAST_Y
    dist = (dx * dx + dy * dy) ** 0.5

    if dist < CURSOR_DEADZONE * SCREEN_W:
        pass
    else:
        if dist / dt > CURSOR_VELOCITY_THRESHOLD * SCREEN_W:
            alpha = CURSOR_VELOCITY_FAST
        else:
            alpha = CURSOR_VELOCITY_SLOW

        _LAST_X += dx * alpha
        _LAST_Y += dy * alpha
        _user32.SetCursorPos(int(_LAST_X), int(_LAST_Y))

    thumb = np.array([landmarks[4].x, landmarks[4].y])
    index = np.array([landmarks[8].x, landmarks[8].y])
    pinch_dist = float(np.linalg.norm(thumb - index))
    is_pinching = pinch_dist < PINCH_DISTANCE

    if is_pinching and not _MOUSE_DOWN:
        pyautogui.mouseDown()
        _MOUSE_DOWN = True
    elif not is_pinching and _MOUSE_DOWN:
        pyautogui.mouseUp()
        _MOUSE_DOWN = False

    if classifier_gesture == "peace" and (now - _LAST_CLICK_TIME) > 0.5:
        _user32.mouse_event(0x0008, 0, 0, 0, 0)
        _user32.mouse_event(0x0010, 0, 0, 0, 0)
        _LAST_CLICK_TIME = now
    elif classifier_gesture == "open_palm" and (now - _LAST_CLICK_TIME) > 0.5:
        _user32.mouse_event(0x0002, 0, 0, 0, 0)
        _user32.mouse_event(0x0004, 0, 0, 0, 0)
        _LAST_CLICK_TIME = now


def release_all():
    global _MOUSE_DOWN
    if _MOUSE_DOWN:
        pyautogui.mouseUp()
        _MOUSE_DOWN = False
