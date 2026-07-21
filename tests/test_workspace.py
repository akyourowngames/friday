import base64
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from ares.models import AppConfig
from ares.workspace.app import create_workspace_app, resolve_workspace_static_dir
from ares.workspace.settings import render_profile, render_soul, workspace_settings
from ares.workspace.uploads import WorkspaceUploadStore


def test_next_workspace_export_and_runtime_descriptor_exist():
    static_dir = resolve_workspace_static_dir()
    assert (static_dir / "index.html").is_file()
    app = create_workspace_app(websocket_host="127.0.0.1", websocket_port=9876)
    with TestClient(app) as client:
        page = client.get("/")
        runtime = client.get("/api/runtime")
        health = client.get("/api/health")
    assert page.status_code == 200
    assert "Ares Power Workspace" in page.text
    assert runtime.json()["websocket_url"] == "ws://127.0.0.1:9876"
    assert health.json()["frontend"] == "nextjs"
    assert page.headers["x-content-type-options"] == "nosniff"


def test_workspace_voice_session_returns_503():
    app = create_workspace_app(voice_config_provider=lambda: None)
    with TestClient(app, client=("127.0.0.1", 40123)) as client:
        response = client.get("/api/voice/session")

    assert response.status_code == 503
    assert "not available" in response.json()["detail"].lower()


def test_workspace_agent_protocol_sends_normalized_selected_session_ids():
    source = (Path(__file__).parents[1] / "ares-workspace" / "app" / "components" / "Workspace.tsx").read_text(encoding="utf-8")
    assert "`conversation-${id}`" in source
    assert 'type: "get_agent_runs", session_id:' in source
    assert 'type: "cancel_agent_run", run_id: runId, session_id:' in source
    assert 'type: "get_artifact", path: artifact.path, session_id:' in source
    assert '"get_status", "get_workspace_settings"' in source
    assert '"get_status", "get_agent_runs"' not in source


def test_bundled_next_export_is_available_without_development_out(tmp_path, monkeypatch):
    from ares.workspace import app as app_module
    monkeypatch.setattr(app_module, "NEXT_WORKSPACE_OUT", tmp_path / "not-built")
    static_dir = app_module.resolve_workspace_static_dir()
    assert static_dir == app_module.BUNDLED_NEXT_DIR
    assert (static_dir / "index.html").is_file()
    assert (static_dir / "_next" / "static").is_dir()


def test_workspace_serves_allowed_pdfs_inline_for_local_preview(tmp_path):
    pdf = tmp_path / "brief.pdf"
    pdf_bytes = b"%PDF-1.6\nlocal preview\n"
    pdf.write_bytes(pdf_bytes)
    app = create_workspace_app(
        artifact_roots=[tmp_path], artifact_resolver=lambda token: pdf if token == "owned" else None
    )
    with TestClient(app) as client:
        response = client.get("/api/artifact", params={"token": "owned"})
    assert response.status_code == 200
    assert response.content == pdf_bytes
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.headers["x-frame-options"] == "SAMEORIGIN"


def test_workspace_rejects_artifacts_outside_approved_roots(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-1.6\n")
    app = create_workspace_app(
        artifact_roots=[allowed], artifact_resolver=lambda token: outside if token == "wrong" else None
    )
    with TestClient(app) as client:
        response = client.get("/api/artifact", params={"token": "wrong"})
    assert response.status_code == 404


def test_workspace_upload_store_persists_and_reuses_safe_files(tmp_path):
    store = WorkspaceUploadStore(tmp_path)
    saved = store.save({
        "name": "../../operator notes.txt",
        "type": "text/plain",
        "data": base64.b64encode(b"watch the authenticated inbox").decode("ascii"),
    })
    assert saved["name"] == "operator notes.txt"
    assert saved["size"] == 29
    assert len(store.list()) == 1
    attachment = store.attachment(saved["id"])
    assert attachment["name"] == "operator notes.txt"
    assert attachment["path"].startswith(str(store.root))
    assert store.delete(saved["id"]) is True
    assert store.list() == []


def test_workspace_upload_store_rejects_invalid_base64(tmp_path):
    store = WorkspaceUploadStore(tmp_path)
    try:
        store.save({"name": "bad.txt", "type": "text/plain", "data": "%%%"})
    except ValueError as exc:
        assert "base64" in str(exc)
    else:
        raise AssertionError("invalid base64 was accepted")


def test_structured_settings_round_trip_profile_and_soul(tmp_path):
    config = AppConfig(data_dir=str(tmp_path), mcp_servers=[])
    profile = render_profile({
        "user_name": "Krish", "pronouns": "he/him", "coding_style": "precise",
        "assistant_style": "direct", "terminal": "Windows / PowerShell",
        "projects": "Ares\nWatcher fleet", "goals": "Ship safely", "notes": "Power user",
    })
    soul = render_soul({
        "assistant_name": "Ares", "personality": "Calm\nDecisive",
        "communication_style": "Lead with the answer", "values": "Privacy first",
        "custom_instructions": "Verify all consequential actions.",
    })
    result = workspace_settings(config, profile, soul)
    assert result["identity"]["user_name"] == "Krish"
    assert result["identity"]["projects"] == "Ares\nWatcher fleet"
    assert result["personalization"]["assistant_name"] == "Ares"
    assert result["personalization"]["custom_instructions"] == "Verify all consequential actions."


def test_empty_profile_fields_do_not_consume_the_next_line(tmp_path):
    config = AppConfig(data_dir=str(tmp_path), mcp_servers=[])
    profile = "# About Me\n\n## Identity\n- Name:\n- Pronouns:\n"
    result = workspace_settings(config, profile, "# Ares - My AI Assistant\n")
    assert result["identity"]["user_name"] == ""
    assert result["identity"]["pronouns"] == ""
