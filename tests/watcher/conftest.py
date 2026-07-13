from __future__ import annotations

import pytest

from ares.watcher.database import WatcherDatabase


@pytest.fixture
def watcher_db(tmp_path):
    db = WatcherDatabase(tmp_path / "watchers.db")
    yield db
    db.close()
