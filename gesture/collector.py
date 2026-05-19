import csv
import time
from pathlib import Path

import cv2
import numpy as np
import mediapipe as mp
from mediapipe.tasks import python
from mediapipe.tasks.python import vision

from .config import GESTURES, DATA_DIR

_MODEL_PATH = Path(__file__).resolve().parent / "hand_landmarker.task"


def _normalize(landmarks):
    wrist = landmarks[0]
    feats = []
    for lm in landmarks:
        feats.extend([lm.x - wrist.x, lm.y - wrist.y, lm.z - wrist.z])
    return feats


def collect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = DATA_DIR / "data.csv"

    existing = set()
    if csv_path.exists():
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                existing.add(int(row[-1]))
    else:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            pass

    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    if not cap.isOpened():
        print("ERROR: Could not open webcam")
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    ret, _ = cap.read()
    if not ret:
        print("ERROR: Webcam opened but no frames received")
        cap.release()
        return

    if not _MODEL_PATH.exists():
        print(f"ERROR: Model file not found at {_MODEL_PATH}")
        cap.release()
        return

    base_options = python.BaseOptions(model_asset_path=str(_MODEL_PATH))
    options = vision.HandLandmarkerOptions(
        base_options=base_options,
        running_mode=vision.RunningMode.VIDEO,
        num_hands=1,
        min_hand_detection_confidence=0.7,
        min_tracking_confidence=0.5,
    )

    current_label = -1
    recording = False
    frames_collected = 0
    target_frames = 200
    frame_count = 0

    print("=== Gesture Data Collector ===")
    print("Keys: 0-7 to select gesture, SPACE to start/stop recording, ESC to quit")
    for i, name in enumerate(GESTURES):
        done = " [DONE]" if i in existing else ""
        print(f"  [{i}] {name}{done}")

    with vision.HandLandmarker.create_from_options(options) as landmarker:
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            frame = cv2.flip(frame, 1)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            ts = int(time.time() * 1000)
            result = landmarker.detect_for_video(mp_image, ts)

            if result.hand_landmarks:
                hls = result.hand_landmarks[0]
                hx = [lm.x for lm in hls]
                hy = [lm.y for lm in hls]

                for i in range(len(hls)):
                    cx, cy = int(hx[i] * frame.shape[1]), int(hy[i] * frame.shape[0])
                    cv2.circle(frame, (cx, cy), 4, (0, 255, 0), -1)

                CONNECTIONS = [
                    (0, 1), (1, 2), (2, 3), (3, 4),
                    (0, 5), (5, 6), (6, 7), (7, 8),
                    (0, 9), (9, 10), (10, 11), (11, 12),
                    (0, 13), (13, 14), (14, 15), (15, 16),
                    (0, 17), (17, 18), (18, 19), (19, 20),
                    (5, 9), (9, 13), (13, 17),
                ]
                for a, b in CONNECTIONS:
                    ax, ay = int(hx[a] * frame.shape[1]), int(hy[a] * frame.shape[0])
                    bx, by = int(hx[b] * frame.shape[1]), int(hy[b] * frame.shape[0])
                    cv2.line(frame, (ax, ay), (bx, by), (0, 255, 0), 2)

                if recording and current_label >= 0:
                    feats = _normalize(hls)
                    with open(csv_path, "a", newline="", encoding="utf-8") as f:
                        writer = csv.writer(f)
                        writer.writerow(feats + [current_label])
                    frames_collected += 1

            overlay = []
            if recording and current_label >= 0:
                label_name = GESTURES[current_label]
                pct = min(frames_collected / target_frames * 100, 100)
                overlay.append(f"RECORDING: {label_name} ({frames_collected}/{target_frames} = {pct:.0f}%)")
            elif current_label >= 0:
                overlay.append(f"Selected: {GESTURES[current_label]} — press SPACE to record")
            else:
                overlay.append("Press 0-7 to select a gesture, then SPACE to record")

            for i, line in enumerate(overlay):
                cv2.putText(frame, line, (10, 30 + i * 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

            cv2.imshow("Gesture Collector", frame)
            key = cv2.waitKey(1) & 0xFF

            if key == 27:
                break
            elif ord("0") <= key <= ord("7"):
                idx = key - ord("0")
                if idx < len(GESTURES):
                    current_label = idx
                    recording = False
                    frames_collected = 0
            elif key == ord(" ") and current_label >= 0:
                recording = not recording
                if recording:
                    frames_collected = 0
                elif frames_collected > 0:
                    print(f"  Stored {frames_collected} frames for {GESTURES[current_label]}")

            if recording and frames_collected >= target_frames:
                print(f"  Done! {frames_collected} frames for {GESTURES[current_label]}")
                recording = False
                frames_collected = 0

            frame_count += 1

    cap.release()
    cv2.destroyAllWindows()

    count = 0
    if csv_path.exists():
        with open(csv_path, newline="", encoding="utf-8") as f:
            count = sum(1 for _ in csv.reader(f))
    print(f"\nTotal samples collected: {count}")
    print("Run /gesture train to train the classifier.")
