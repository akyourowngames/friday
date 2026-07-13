import sys

from ares import __main__ as main_module


def test_all_flag_starts_unified_runtime_with_dashboard_overrides(monkeypatch):
    captured = {}

    async def fake_run_server(host, port, *, watcher_host=None, watcher_port=None,
                              workspace_host=None, workspace_port=None):
        captured.update(host=host, port=port, watcher_host=watcher_host, watcher_port=watcher_port,
                        workspace_host=workspace_host, workspace_port=workspace_port)

    monkeypatch.setattr(main_module, "_run_server", fake_run_server)
    monkeypatch.setattr(sys, "argv", [
        "ares", "--all", "--host", "127.0.0.2", "--port", "9000",
        "--watcher-host", "127.0.0.3", "--watcher-port", "9001",
    ])

    main_module.main()

    assert captured == {
        "host": "127.0.0.2",
        "port": 9000,
        "watcher_host": "127.0.0.3",
        "watcher_port": 9001,
        "workspace_host": None,
        "workspace_port": None,
    }


def test_combined_legacy_surface_flags_route_to_one_runtime(monkeypatch):
    calls = []

    async def fake_run_server(host, port, *, watcher_host=None, watcher_port=None,
                              workspace_host=None, workspace_port=None):
        calls.append((host, port, watcher_host, watcher_port))

    async def forbidden_telegram():
        raise AssertionError("focused Telegram mode must not start")

    monkeypatch.setattr(main_module, "_run_server", fake_run_server)
    monkeypatch.setattr(main_module, "_run_telegram", forbidden_telegram)
    monkeypatch.setattr(sys, "argv", ["ares", "--server", "--telegram", "--watcher"])

    main_module.main()

    assert calls == [("127.0.0.1", 8765, None, None)]


def test_legacy_telegram_flag_also_routes_to_unified_runtime(monkeypatch):
    calls = []

    async def fake_run_server(host, port, *, watcher_host=None, watcher_port=None,
                              workspace_host=None, workspace_port=None):
        calls.append((host, port))

    monkeypatch.setattr(main_module, "_run_server", fake_run_server)
    monkeypatch.setattr(sys, "argv", ["ares", "--telegram"])
    main_module.main()
    assert calls == [("127.0.0.1", 8765)]
