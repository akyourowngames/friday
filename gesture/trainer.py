import csv
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, accuracy_score
from sklearn.preprocessing import StandardScaler

from .config import GESTURES, DATA_DIR, MODEL_PATH, SCALER_PATH


def train():
    csv_path = DATA_DIR / "data.csv"

    if not csv_path.exists():
        print("No training data found. Run /gesture collect first.")
        return

    X, y = [], []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) < 64:
                continue
            X.append([float(v) for v in row[:-1]])
            y.append(int(row[-1]))

    if len(X) < 100:
        print(f"Not enough data ({len(X)} samples). Collect more with /gesture collect.")
        return

    X = np.array(X)
    y = np.array(y)

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    X_train, X_test, y_train, y_test = train_test_split(
        X_scaled, y, test_size=0.2, random_state=42, stratify=y
    )

    clf = RandomForestClassifier(n_estimators=200, random_state=42, n_jobs=-1)
    clf.fit(X_train, y_train)

    y_pred = clf.predict(X_test)
    acc = accuracy_score(y_test, y_pred)

    print(f"\nAccuracy: {acc:.2%}")
    print()
    print(classification_report(y_test, y_pred,
                                target_names=[GESTURES[i] for i in sorted(set(y))],
                                zero_division=0))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(clf, MODEL_PATH)
    joblib.dump(scaler, SCALER_PATH)
    print(f"Model saved to {MODEL_PATH}")
    print(f"Scaler saved to {SCALER_PATH}")
    print("Run /gesture start to begin live detection.")
