from ares.desktop.history import HistoryStore


def test_history_store_add_and_get():
    store = HistoryStore(max_size=3)
    store.add("Hello Ares", "Hi there!")
    store.add("What time is it?", "It's 3 PM.")
    entries = store.recent()
    assert len(entries) == 2
    assert entries[0]["user"] == "Hello Ares"
    assert entries[0]["assistant"] == "Hi there!"


def test_history_store_respects_max_size():
    store = HistoryStore(max_size=2)
    store.add("q1", "a1")
    store.add("q2", "a2")
    store.add("q3", "a3")
    entries = store.recent()
    assert len(entries) == 2
    assert entries[0]["user"] == "q2"
    assert entries[1]["user"] == "q3"


def test_history_store_empty():
    store = HistoryStore(max_size=5)
    assert store.recent() == []


def test_history_store_recent_limit():
    store = HistoryStore(max_size=10)
    for i in range(5):
        store.add(f"q{i}", f"a{i}")
    entries = store.recent(limit=3)
    assert len(entries) == 3
    assert entries[0]["user"] == "q2"
