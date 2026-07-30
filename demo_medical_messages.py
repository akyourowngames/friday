"""Demo: test the medical classifier against sample Telegram messages.

Run: python demo_medical_messages.py
"""

import asyncio
import json

from ares.channels.medical_classifier import MedicalClassifier


async def main():
    classifier = MedicalClassifier()

    test_messages = [
        # --- Should be classified as MEDICAL ---
        {
            "label": "Lab report (clear positive)",
            "text": "Lab report for patient #4512 — blood glucose 280, BP 160/100",
            "attachment": None,
        },
        {
            "label": "Discharge summary",
            "text": "Discharge summary for Mrs. Garcia. Admitted 12 Jul, discharged today. Dx: Acute MI, secondary diabetes.",
            "attachment": {"name": "discharge_garcia.pdf", "type": "application/pdf", "size": 45000, "kind": "file"},
        },
        {
            "label": "Critical ICU report",
            "text": "Critical ICU patient #7 — sepsis, blood culture positive, WBC 18000. Start broad-spectrum abx STAT.",
            "attachment": None,
        },
        {
            "label": "X-ray results (attachment only)",
            "text": "",
            "attachment": {"name": "chest_xray_report.pdf", "type": "application/pdf", "size": 120000, "kind": "file"},
        },
        {
            "label": "Prescription",
            "text": "Rx: Metformin 500mg BD, Lisinopril 10mg OD, Atorvastatin 20mg HS. Follow up in 2 weeks.",
            "attachment": None,
        },
        {
            "label": "MRI report (ambiguous text + medical file)",
            "text": "Here's the update from Dr. Patel",
            "attachment": {"name": "mri_brain_scan.pdf", "type": "application/pdf", "size": 200000, "kind": "file"},
        },
        {
            "label": "Routine follow-up",
            "text": "Routine follow-up lab results — HbA1c 7.2%, fasting glucose 145. Continue current meds.",
            "attachment": None,
        },

        # --- Should be classified as NOT MEDICAL ---
        {
            "label": "Casual greeting",
            "text": "hey what's up",
            "attachment": None,
        },
        {
            "label": "Meeting安排",
            "text": "Can we meet at 3pm tomorrow? I'll bring the documents.",
            "attachment": None,
        },
        {
            "label": "Food photo",
            "text": "Look at this lunch!",
            "attachment": {"name": "lunch_photo.jpg", "type": "image/jpeg", "size": 2000000, "kind": "image"},
        },
        {
            "label": "Generic question",
            "text": "Can you look into this for me?",
            "attachment": None,
        },
        {
            "label": "Code file",
            "text": "Check this script",
            "attachment": {"name": "server.py", "type": "text/x-python", "size": 5000, "kind": "file"},
        },
    ]

    print("=" * 70)
    print("  MEDICAL REPORT CLASSIFIER — Demo")
    print("=" * 70)

    for i, msg in enumerate(test_messages, 1):
        result = await classifier.classify(msg["text"], msg["attachment"])

        icon = "✅" if result.is_medical else "❌"
        expected = "MEDICAL" if i <= 7 else "NOT MEDICAL"
        status = "CORRECT" if (
            (result.is_medical and expected == "MEDICAL")
            or (not result.is_medical and expected == "NOT MEDICAL")
        ) else "WRONG"

        print(f"\n{'─' * 70}")
        print(f"  [{i:2d}] {msg['label']}")
        print(f"  Text: {msg['text'][:80] or '(empty)'}")
        if msg["attachment"]:
            print(f"  File: {msg['attachment']['name']} ({msg['attachment']['type']})")
        print(f"  Result: {icon} {'MEDICAL' if result.is_medical else 'NOT MEDICAL'} "
              f"(confidence: {result.confidence:.0%}, source: {result.source})")
        print(f"  Category: {result.category} | Urgency: {result.urgency}")
        if result.summary:
            print(f"  Summary: {result.summary}")
        print(f"  Expected: {expected} — {status}")

    print(f"\n{'=' * 70}")
    print("  Done.")


if __name__ == "__main__":
    asyncio.run(main())
