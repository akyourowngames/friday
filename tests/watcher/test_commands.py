import pytest

from ares.goals import GoalStore
from ares.watcher.commands import WatcherCommands, parse_interval


@pytest.mark.parametrize("value,seconds", [("20s",20),("15m",900),("2h",7200),("1d",86400),("60",60)])
def test_parse_interval(value,seconds): assert parse_interval(value)==seconds


def test_command_lifecycle(tmp_path):
    commands=WatcherCommands(tmp_path/"commands.db")
    try:
        added=commands.execute('add "Production API" https://example.com --interval 5m --type custom')
        identifier=added["monitor"]["id"][:8]
        assert added["monitor"]["interval_seconds"]==300
        assert commands.execute("list")["monitors"][0]["name"]=="Production API"
        assert commands.execute(f"pause {identifier}")["monitor"]["enabled"] is False
        assert commands.execute(f"resume {identifier}")["monitor"]["enabled"] is True
        assert commands.execute(f"status {identifier}")["monitor"]["type"]=="custom"
        assert commands.execute(f"remove {identifier}")["action"]=="remove"
        assert commands.execute("list")["monitors"]==[]
    finally: commands.close()


def test_command_validation(tmp_path):
    commands=WatcherCommands(tmp_path/"commands.db")
    try:
        with pytest.raises(ValueError): commands.execute("add missing-url")
        with pytest.raises(ValueError): commands.execute("pause nope")
        with pytest.raises(ValueError): parse_interval("5s")
    finally: commands.close()


def test_command_can_link_goal_and_reports_it_in_status(tmp_path):
    goals = GoalStore(tmp_path / "ares.db")
    goal = goals.create("Keep production healthy")
    commands = WatcherCommands(tmp_path / "commands.db", goal_store=goals)
    try:
        added = commands.execute(
            f'add "Production API" https://example.com --interval 5m --goal {goal["goal_id"]}'
        )
        assert added["linked_goal_id"] == goal["goal_id"]
        identifier = added["monitor"]["id"][:8]
        status = commands.execute(f"status {identifier}")
        assert status["linked_goals"][0]["goal_id"] == goal["goal_id"]
        removed = commands.execute(f"remove {identifier}")
        assert removed["unlinked_goal_ids"] == [goal["goal_id"]]
    finally:
        commands.close()
        goals.close()
