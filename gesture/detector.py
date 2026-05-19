import time
import threading
import queue
from pathlib import Path

import cv2
import joblib
import numpy as np
import mediapipe as mp
from mediapipe.tasks import python
from mediapipe.tasks.python import vision

from .config import GESTURES, CONFIDENCE_THRESHOLD, SMOOTH_FRAMES, COOLDOWN_MS, MODEL_PATH, SCALER_PATH

_MODEL_PATH = Path(__file__).resolve().parent / "hand_landmarker.task"

HAND_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (0, 9), (9, 10), (10, 11), (11, 12),
    (0, 13), (13, 14), (14, 15), (15, 16),
    (0, 17), (17, 18), (18, 19), (19, 20),
    (5, 9), (9, 13), (13, 17),
]


class GestureDetector:
    def __init__(self):
        self._running = False
        self._paused = False
        self._thread = None
        self._action_queue = queue.Queue()
        self._cap = None

        self._model = None
        self._scaler = None
        if MODEL_PATH.exists():
            self._model = joblib.load(MODEL_PATH)
        if SCALER_PATH.exists():
            self._scaler = joblib.load(SCALER_PATH)

        self._last_gesture = -1
        self._streak = 0
        self._last_trigger_time = 0.0
        self._current_gesture_name = "none"

    @property
    def current_gesture(self):
        return self._current_gesture_name

    def _normalize(self, landmarks):
        wrist = landmarks[0]
        feats = []
        for lm in landmarks:
            feats.extend([lm.x - wrist.x, lm.y - wrist.y, lm.z - wrist.z])
        return np.array(feats).reshape(1, -1)

    def get_action(self, timeout=0.05):
        try:
            return self._action_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def start(self):
        if self._running:
            return
        if self._model is None:
            print("No model found. Run /gesture train first.")
            return

        if not _MODEL_PATH.exists():
            print(f"ERROR: Model file not found at {_MODEL_PATH}")
            return

        self._cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        if not self._cap.isOpened():
            print("ERROR: Could not open webcam")
            self._cap = None
            return

        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

        ret, _ = self._cap.read()
        if not ret:
            print("ERROR: Webcam opened but no frames received")
            self._cap.release()
            self._cap = None
            return

        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        if self._cap:
            self._cap.release()
            self._cap = None
        cv2.destroyAllWindows()
        from .mouse import release_all
        release_all()

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def _loop(self):
        base_options = python.BaseOptions(model_asset_path=str(_MODEL_PATH))
        options = vision.HandLandmarkerOptions(
            base_options=base_options,
            running_mode=vision.RunningMode.VIDEO,
            num_hands=1,
            min_hand_detection_confidence=0.5,
            min_tracking_confidence=0.5,
        )

        with vision.HandLandmarker.create_from_options(options) as landmarker:
            while self._running:
                if self._paused:
                    time.sleep(0.1)
                    continue

                ret, frame = self._cap.read()
                if not ret:
                    time.sleep(0.01)
                    continue

                frame = cv2.flip(frame, 1)
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                ts = int(time.time() * 1000)
                result = landmarker.detect_for_video(mp_image, ts)

                gesture_idx = -1
                confidence = 0.0
                hls = None

                if result.hand_landmarks:
                    hls = result.hand_landmarks[0]
                    hx = [lm.x for lm in hls]
                    hy = [lm.y for lm in hls]

                    for i in range(len(hls)):
                        cx, cy = int(hx[i] * frame.shape[1]), int(hy[i] * frame.shape[0])
                        cv2.circle(frame, (cx, cy), 3, (0, 255, 0), -1)

                    for a, b in HAND_CONNECTIONS:
                        ax, ay = int(hx[a] * frame.shape[1]), int(hy[a] * frame.shape[0])
                        bx, by = int(hx[b] * frame.shape[1]), int(hy[b] * frame.shape[0])
                        cv2.line(frame, (ax, ay), (bx, by), (0, 255, 0), 1)

                    feats = self._normalize(hls)
                    if self._scaler:
                        feats = self._scaler.transform(feats)
                    pred = self._model.predict(feats)[0]
                    proba = self._model.predict_proba(feats)[0]
                    confidence = float(np.max(proba))
                    gesture_idx = int(pred)

                # --- ALWAYS run cursor tracking when hand detected ---
                if hls is not None:
                    from .mouse import process_mouse
                    gesture_name = GESTURES[gesture_idx] if gesture_idx >= 0 and confidence >= CONFIDENCE_THRESHOLD else ""
                    process_mouse(hls, gesture_name)

                # --- ALWAYS run nav action queue ---
                if confidence >= CONFIDENCE_THRESHOLD and gesture_idx >= 0:
                    if gesture_idx == self._last_gesture:
                        self._streak += 1
                    else:
                        self._streak = 0
                    self._last_gesture = gesture_idx

                    if self._streak >= SMOOTH_FRAMES:
                        now = time.time()
                        if (now - self._last_trigger_time) * 1000 >= COOLDOWN_MS:
                            gesture_name = GESTURES[gesture_idx]
                            if gesture_name != "none":
                                self._action_queue.put(gesture_name)
                            self._last_trigger_time = now
                            self._streak = 0
                else:
                    self._streak = 0
                    self._last_gesture = -1

                label = GESTURES[gesture_idx] if gesture_idx >= 0 else "none"
                self._current_gesture_name = label
                hand_status = "hand" if hls is not None else "no hand"
                cv2.putText(frame, f"{label} | cursor {hand_status}", (10, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

                cv2.imshow("Gesture Control", frame)
                if cv2.waitKey(1) & 0xFF == 27:
                    self._running = False
                    break
