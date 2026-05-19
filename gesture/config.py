from pathlib import Path

GESTURES = ["none", "open_palm", "fist", "point", "peace", "pinch", "thumbs_up", "thumbs_down"]
CONFIDENCE_THRESHOLD = 0.9
SMOOTH_FRAMES = 3
COOLDOWN_MS = 500
DATA_DIR = Path("storage/gesture_data")
MODEL_PATH = Path("storage/gesture_model.pkl")
SCALER_PATH = Path("storage/gesture_scaler.pkl")

# Mouse mode
PINCH_DISTANCE = 0.05
CURSOR_DEADZONE = 0.005
CURSOR_VELOCITY_SLOW = 0.3
CURSOR_VELOCITY_FAST = 0.8
CURSOR_VELOCITY_THRESHOLD = 0.02
