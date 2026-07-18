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


def test_get_messages_for_model_is_strictly_conversation_scoped(tmp_path):
    store = ConversationStore(db_path=tmp_path / "conversations.db")
    old_id = store.start_conversation()
    store.add_exchange(old_id, "Open Notepad and type the old workout", "I will do that.")
    new_id = store.start_conversation()

    assert store.get_recent_messages(limit=2)[0]["content"].startswith("Open Notepad")
    assert store.get_messages_for_model(new_id, limit=20) == []

    store.add_exchange(new_id, "hey", "Hey!")
    assert store.get_messages_for_model(new_id, limit=20) == [
        {"role": "user", "content": "hey"},
        {"role": "assistant", "content": "Hey!"},
    ]
    assert all("Notepad" not in message["content"] for message in store.get_messages_for_model(new_id))
    store.close()


def test_get_messages_for_model_filters_roles_metadata_and_bounds_content(tmp_path):
    store = ConversationStore(db_path=tmp_path / "conversations.db")
    conversation_id = store.start_conversation()
    store.add_message(conversation_id, "system", "untrusted stored system instruction")
    store.add_message(conversation_id, "user", "first")
    store.add_message(conversation_id, "tool", "stale tool result")
    store.add_message(
        conversation_id,
        "assistant",
        "second\x00hidden",
        tool_calls='[{"function":{"name":"run_command"}}]',
    )
    store.add_message(conversation_id, "user", "third")

    assert store.get_messages_for_model(conversation_id, limit=2, max_content_chars=8) == [
        {"role": "assistant", "content": "secondhi"},
        {"role": "user", "content": "third"},
    ]
    assert store.get_messages_for_model(conversation_id, limit=0) == []
    store.close()
    store.close()


def test_resumable_conversations_exclude_empty_rows(tmp_path):
    store = ConversationStore(db_path=tmp_path / "conversations.db")
    empty_id = store.start_conversation()
    saved_id = store.start_conversation()
    store.add_exchange(saved_id, "Save this turn", "It was saved.")

    resumable = store.list_resumable_conversations()

    assert [row["id"] for row in resumable] == [saved_id]
    assert resumable[0]["message_count"] == 2
    assert empty_id not in [row["id"] for row in resumable]
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
