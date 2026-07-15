from ares.commitments import CommitmentStore


def test_commitments_are_deduplicated_and_track_completion(tmp_path):
    store = CommitmentStore(tmp_path / "commitments.db")
    first = store.create(
        "Send the launch notes",
        owner="ares",
        due_at="2026-07-20T09:00:00Z",
        confidence=0.9,
    )
    duplicate = store.create("Send the launch notes", owner="ares")

    assert duplicate["commitment_id"] == first["commitment_id"]
    assert store.list_pending()[0]["due_at"] == "2026-07-20T09:00:00Z"

    completed = store.complete(first["commitment_id"])
    assert completed["status"] == "completed"
    assert completed["completed_at"]
    assert store.list_pending() == []
    store.close()


def test_commitment_export_import_preserves_activity(tmp_path):
    source = CommitmentStore(tmp_path / "source.db")
    commitment = source.create("Review the Ares plan", owner="user", confidence=0.85)
    source.mark_reminded(commitment["commitment_id"], when="2026-07-15T08:00:00Z")

    target = CommitmentStore(tmp_path / "target.db")
    assert target.import_commitments(source.list_all_for_export()) == 1
    restored = target.list_pending()[0]
    assert restored["description"] == "Review the Ares plan"
    assert restored["last_reminder_at"] == "2026-07-15T08:00:00Z"

    source.close()
    target.close()
