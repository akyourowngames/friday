"""Tests for persistent conversations."""

from ares.conversations import ConversationStore


def test_store_messages_and_summarize(tmp_path):
    store = ConversationStore(db_path=tmp_path / "conversations.db")
    conversation_id = store.start_conversation()

    store.add_exchange(conversation_id, "remember my dark mode preference", "Stored it.")
    store.end_conversation(conversation_id)
    summary = store.summarize_conversation(conversation_id)

    assert "dark mode" in summary
    recent = store.get_recent_messages(limit=2)
    assert recent == [
        {"role": "user", "content": "remember my dark mode preference"},
        {"role": "assistant", "content": "Stored it."},
    ]
    assert store.get_recent_summaries(limit=1) == [summary]
    store.close()


def test_summarize_ended_without_summary(tmp_path):
    store = ConversationStore(db_path=tmp_path / "conversations.db")
    conversation_id = store.start_conversation()
    store.add_exchange(conversation_id, "hello", "hi")
    store.end_conversation(conversation_id)

    assert store.summarize_ended_without_summary(min_messages=2) == 1
    assert store.summarize_ended_without_summary(min_messages=2) == 0
    store.close()


def test_local_conversation_recall_indexes_existing_and_new_messages(tmp_path):
    database = tmp_path / "conversations.db"
    store = ConversationStore(db_path=database)
    conversation_id = store.start_conversation()
    store.add_message(conversation_id, "user", "We finished the Orion planning file yesterday.")
    store.close()

    # Re-opening rebuilds the FTS index for data created before the index was
    # available, then newly appended messages are indexed immediately.
    store = ConversationStore(db_path=database)
    recalled = store.search_recall("Orion")
    assert recalled and recalled[0]["content"].startswith("We finished the Orion")

    store.add_message(conversation_id, "assistant", "The plan is ready for the next step.")
    recent = store.search_recall("", limit=2)
    assert [item["role"] for item in recent] == ["assistant", "user"]
    store.close()
