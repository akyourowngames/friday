"""Test memory system: brain.py logic and file persistence."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.brain import _is_vague, _contradiction_category, _normalize_fact

print("=" * 60)
print("MEMORY SYSTEM TESTS")
print("=" * 60)

all_pass = True

# ─── 1. Quality filter tests ───
print("\n[1] Quality filter (_is_vague):")
tests = [
    ("short", True, "less than 15 chars"),
    ("User has medical records", True, "vague medical"),
    ("doctor's offices nearby", True, "vague doctor"),
    ("User lives in an area with available", True, "vague area"),
    ("User listened to Despacito", True, "listened to"),
    ("User name is Krish Verma", False, "valid name"),
    ("User lives in Delhi", False, "valid location"),
    ("User is 15 years old", False, "valid age"),
    ("User has recovered from illness", False, "valid recovery"),
]
for text, expect, desc in tests:
    result = _is_vague(text)
    ok = result == expect
    status = "PASS" if ok else "FAIL"
    if not ok:
        all_pass = False
    print(f"  {status}: '{text}' -> {result} (expected {expect}) [{desc}]")

# ─── 2. Contradiction category tests ───
print("\n[2] Contradiction patterns:")
tests = [
    ("User name is Krish", True),
    ("User name is Krish Verma", True),
    ("User lives in Delhi", True),
    ("User lives in Maharashtra", True),
    ("User is 15 years old", True),
    ("User age is 15", True),
    ("User is not feeling well", True),
    ("User feels sick", True),
    ("User has recovered from illness", True),
    ("User now lives in Delhi", True),
    ("Hello world", False),
    ("The weather is nice", False),
]
for text, expect in tests:
    result = _contradiction_category(text) is not None
    ok = result == expect
    status = "PASS" if ok else "FAIL"
    if not ok:
        all_pass = False
    print(f"  {status}: '{text}' -> {result}")

# ─── 3. Normalize fact tests ───
print("\n[3] Normalize fact:")
tests = [
    ("User now lives in Delhi", "User lives in Delhi"),
    ("User actually lives in Mumbai", "User lives in Mumbai"),
    ("User currently lives in Pune", "User lives in Pune"),
    ("User name is Krish", "User name is Krish"),
]
for text, expected in tests:
    result = _normalize_fact(text)
    ok = result == expected
    status = "PASS" if ok else "FAIL"
    if not ok:
        all_pass = False
    print(f"  {status}: '{text}' -> '{result}' (expected '{expected}')")

# ─── 4. Brain integration: commit + contradiction removal ───
print("\n[4] Brain integration (commit + contradiction removal):")
from memory.brain import Brain
import numpy as np

def _fake_embed(texts):
    if isinstance(texts, str):
        return np.ones(384, dtype=np.float32)
    return np.ones((len(texts), 384), dtype=np.float32)

import memory.brain as brain_mod
brain_mod.embed = _fake_embed

b = Brain()
b.memories = []

b.commit("User lives in Maharashtra", importance=0.5)
ok = len([m for m in b.memories if "Maharashtra" in m["text"]]) == 1
print(f"  {'PASS' if ok else 'FAIL'}: stored 'Maharashtra' -> {len(b.memories)} mems")
if not ok: all_pass = False

b.commit("User lives in Delhi", importance=0.5)
delhi = [m for m in b.memories if "Delhi" in m["text"]]
maha = [m for m in b.memories if "Maharashtra" in m["text"]]
ok = len(delhi) == 1 and len(maha) == 0
print(f"  {'PASS' if ok else 'FAIL'}: 'Delhi' replaces 'Maharashtra' -> Delhi:{len(delhi)} Maha:{len(maha)}")
if not ok: all_pass = False

b.commit("User is not feeling well", importance=0.5)
b.commit("User has recovered", importance=0.5)
recovered = [m for m in b.memories if "recovered" in m["text"]]
sick = [m for m in b.memories if "not feeling" in m["text"]]
ok = len(recovered) == 1 and len(sick) == 0
print(f"  {'PASS' if ok else 'FAIL'}: 'recovered' removes 'not feeling' -> recovered:{len(recovered)} sick:{len(sick)}")
if not ok: all_pass = False

before = len(b.memories)
b.commit("User has medical records", importance=0.5)
after = len(b.memories)
ok = after == before
print(f"  {'PASS' if ok else 'FAIL'}: vague 'medical records' rejected -> {after} == {before}")
if not ok: all_pass = False

b.commit("User lives in Delhi", importance=0.5)
after2 = len(b.memories)
ok = after2 == after
print(f"  {'PASS' if ok else 'FAIL'}: duplicate 'Delhi' rejected -> {after2} == {after}")
if not ok: all_pass = False

b.commit("User name is Krish", importance=0.5)
b.commit("User name is Krish Verma", importance=0.5)
krish = [m for m in b.memories if "Krish" in m["text"]]
ok = len(krish) == 1 and "Krish Verma" in krish[0]["text"]
print(f"  {'PASS' if ok else 'FAIL'}: 'Krish Verma' replaces 'Krish' -> {[m['text'] for m in krish]}")
if not ok: all_pass = False

# ─── 5. Verify file persistence ───
print("\n[5] File persistence check:")
files = sorted(Path("storage/memories").glob("memory_*.json"))
print(f"  Memory files: {[f.name for f in files]}")
for f in files:
    data = json.loads(f.read_text(encoding="utf-8"))
    for item in data:
        if _is_vague(item["text"]):
            print(f"  WARNING: {f.name} contains vague fact: {item['text']}")
            all_pass = False

# ─── Summary ───
print()
print("=" * 60)
if all_pass:
    print("ALL TESTS PASSED")
else:
    print("SOME TESTS FAILED")
print("=" * 60)
