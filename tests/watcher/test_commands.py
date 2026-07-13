import pytest

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
