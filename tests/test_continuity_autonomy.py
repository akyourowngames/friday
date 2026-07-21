"""Regression and local end-to-end coverage for continuity/autonomy upgrades."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from rich.console import Console

from ares.skills.actions import ActionLedger
from ares.agent import Agent
from ares.autonomy import AutonomousWorkflowRunner
from ares.context.conversations import ConversationStore
from ares.memory import MemoryStore
from ares.models import AppConfig, PhoneConfig
from ares.memory.people import PeopleStore, PersonConflictError
from ares.sessions import SessionStore
from ares.skills.tasks import TaskConflictError, TaskStore, TaskToolHandlers
from ares.tools import ToolExecutor
from ares.tools.renders import get_renderer
from ares.integrations.turn_policy import build_turn_execution_context


def test_people_store_resolves_exact_aliases_and_returns_complete_records(tmp_path):
    store = PeopleStore(tmp_path / "ares.db")
    try:
        created = store.create(
            "Priya Sharma",
            aliases=["mom", "Priya aunty"],
            relation="mother",
            phone="+15555550123",
            email="priya@example.test",
            notes="private family note",
        )
        assert store.resolve("mom", require="phone")["phone"] == "+15555550123"
        assert store.resolve("Priya aunty", require="email")["email"] == "priya@example.test"

        record = store.search("mom", include_sensitive=True)[0]
        assert record["phone"] == "+15555550123"
        assert record["email"] == "priya@example.test"
        assert record["notes"] == "private family note"

        assert store.mark_contacted("mom", channel="email") is True
        contacted = store.search("mom", include_sensitive=True)[0]
        assert contacted["last_contacted_via"] == "email"
        assert contacted["last_contacted_at"]

        with pytest.raises(PersonConflictError):
            store.create("Different Person", aliases=["mom"])
        with pytest.raises(PersonConflictError):
            store.update(created["person_id"], relation="parent", expected_revision=999)
    finally:
        store.close()


def test_executor_returns_complete_people_records_and_resolves_sms_alias(tmp_path, fake_embedding_provider, monkeypatch):
    memory = MemoryStore(db_path=tmp_path / "ares.db", embedding_provider=fake_embedding_provider)
    executor = ToolExecutor(memory, config=AppConfig(data_dir=str(tmp_path), phone=PhoneConfig(enabled=True)))
    calls: list[tuple[str, str]] = []

    def fake_send(number: str, message: str) -> str:
        calls.append((number, message))
        return json.dumps({"ok": True, "sent": True, "number": number, "error": ""})

    monkeypatch.setattr("ares.tools.executor._kdeconnect_bridge.send_sms", fake_send)
    try:
        remembered = json.loads(executor.execute("remember_person", {
            "canonical_name": "Rohan Patel", "aliases": ["bro"], "phone": "+15555550999",
        }))
        assert remembered["ok"] is True
        assert remembered["person"]["phone"] == "+15555550999"

        updated = json.loads(executor.execute("update_person", {
            "person_id": remembered["person"]["person_id"], "relation": "brother",
        }))
        assert updated["person"]["relation"] == "brother"

        result = executor.execute("phone_send_sms", {"number": "bro", "message": "private message body"})
        assert calls == [("+15555550999", "private message body")]
        assert "+15555550999" in result
        assert "private message body" not in result
        assert "Rohan Patel" in result

        records = executor.action_ledger.list_all()
        sms = next(record for record in records if record["action_type"] == "sms_sent")
        assert sms["target"] == "bro"
        assert "private message body" not in sms["summary"] + sms["target"]
    finally:
        executor.close()
        memory.close()


def test_action_ledger_records_provenance_not_file_content_and_supports_relative_since(tmp_path, fake_embedding_provider):
    memory = MemoryStore(db_path=tmp_path / "ares.db", embedding_provider=fake_embedding_provider)
    executor = ToolExecutor(memory)
    target = tmp_path / "private-note.txt"
    secret = "do not duplicate this private text"
    try:
        result = executor.execute("write_file", {"path": str(target), "content": secret})
        assert "wrote" in result.casefold() or "created" in result.casefold()
        records = executor.action_ledger.search("Wrote", since="yesterday")
        assert len(records) == 1
        assert records[0]["action_type"] == "file_created"
        assert records[0]["target"].endswith("private-note.txt")
        assert secret not in json.dumps(records)
    finally:
        executor.close()
        memory.close()


def test_people_and_action_renderers_show_complete_local_people_records():
    console = Console(record=True, width=120)
    people_payload = json.dumps({
        "ok": True,
        "people": [{"person_id": 1, "canonical_name": "Asha Mehta", "relation": "friend", "aliases": ["asha"], "phone": "+15555550123", "email": "asha@example.test", "notes": "college friend"}],
    })
    console.print(get_renderer("search_person")(people_payload))
    people_text = console.export_text()
    assert "Asha Mehta" in people_text
    assert "+15555550123" in people_text
    assert "asha@example.test" in people_text

    action_payload = json.dumps({
        "ok": True,
        "actions": [{"created_at": "2026-07-11T10:00:00Z", "action_type": "file_created", "summary": "Wrote a local file.", "target": "notes.md"}],
    })
    console = Console(record=True, width=120)
    console.print(get_renderer("search_actions")(action_payload))
    assert "Wrote a local file." in console.export_text()


def test_export_full_includes_people_and_people_profile_remains_selective(tmp_path, fake_embedding_provider):
    database = tmp_path / "ares.db"
    memory = MemoryStore(db_path=database, embedding_provider=fake_embedding_provider)
    people = PeopleStore(database)
    actions = ActionLedger(database)
    try:
        people.create("Maya Singh", aliases=["maya"], phone="+15555550111", email="maya@example.test")
        actions.record("file_created", target="notes.txt", summary="Wrote a local file.", tool_name="write_file")
        from ares.tools.exporter import export_data

        full = export_data(memory_store=memory, people_store=people, action_ledger=actions, path=tmp_path / "full.json")
        full_payload = json.loads(full.read_text(encoding="utf-8"))
        assert full_payload["people"][0]["phone"] == "+15555550111"
        assert full_payload["actions"]

        people_export = export_data(memory_store=memory, people_store=people, action_ledger=actions, path=tmp_path / "people.json", profile="people")
        people_payload = json.loads(people_export.read_text(encoding="utf-8"))
        assert people_payload["people"][0]["phone"] == "+15555550111"
        assert people_payload["actions"] == []
    finally:
        actions.close()
        people.close()
        memory.close()


def test_agent_context_includes_complete_people_and_relevant_action_history(tmp_path, fake_embedding_provider):
    data_dir = tmp_path / "data"
    memory = MemoryStore(db_path=data_dir / "ares.db", embedding_provider=fake_embedding_provider)
    agent = Agent(memory, config=AppConfig(data_dir=str(data_dir)))
    try:
        agent.people_store.create(
            "Nina Shah", aliases=["nina"], relation="friend", phone="+15555550777",
            email="nina@example.test", notes="never expose this note",
        )
        agent.action_ledger.record(
            "file_created", target="C:/work/brief.md", summary="Wrote a local file.", tool_name="write_file"
        )
        context = agent.get_context("Can you find that file from 5 days ago?")
        assert "Nina Shah" in context and "friend" in context
        assert "+15555550777" in context and "nina@example.test" in context
        assert "never expose this note" in context
        assert "brief.md" in context
    finally:
        agent.tool_executor.close()
        memory.close()


def test_agent_continuation_recalls_saved_alias_and_raw_prior_chat(tmp_path, fake_embedding_provider):
    data_dir = tmp_path / "data"
    database = data_dir / "ares.db"
    memory = MemoryStore(db_path=database, embedding_provider=fake_embedding_provider)
    conversations = ConversationStore(database)
    agent = Agent(memory, conversation_store=conversations, config=AppConfig(data_dir=str(data_dir)))
    try:
        agent.people_store.create("Rohit", aliases=["rohit"], email="rohit@example.test")
        conversation_id = conversations.start_conversation()
        conversations.add_exchange(
            conversation_id,
            "Please send the project note to Rohit at rohit@example.test.",
            "I will use the saved contact alias after confirmation.",
        )
        context = agent.get_context("Continue the email to Rohit from 5 days ago")
        assert "Rohit" in context
        assert "rohit@example.test" in context
        assert "Relevant Prior Conversation" in context
        assert "[redacted email]" not in context
        assert "conversation:" in context
    finally:
        agent.tool_executor.close()
        conversations.close()
        memory.close()


def test_unified_memory_search_reads_jsonl_sessions_and_returns_source_ids(tmp_path, fake_embedding_provider):
    data_dir = tmp_path / "data"
    database = data_dir / "ares.db"
    memory = MemoryStore(db_path=database, embedding_provider=fake_embedding_provider)
    conversations = ConversationStore(database)
    sessions = SessionStore(data_dir)
    executor = ToolExecutor(memory, conversation_store=conversations, session_store=sessions)
    try:
        memory.store("Rohit uses Instagram for project updates.", category="relationship")
        executor.people_store.create("Rohit Verma", aliases=["rohit"], email="rohit@example.test")
        conversation_id = conversations.start_conversation()
        conversations.add_message(conversation_id, "user", "Rohit said his project channel is Instagram.")
        sessions.write_message("sess-rohit", "user", "Rohit Verma is my cousin.")
        sessions.write_message("sess-rohit", "assistant", "His Instagram ID is @rohit_dev_42.")

        payload = json.loads(executor.execute("search_memory", {"query": "Rohit Instagram", "limit": 12}))

        assert payload["ok"] is True
        assert payload["counts"]["sessions"] >= 1
        session_record = next(record for record in payload["results"] if record["source"] == "session")
        assert session_record["source_id"].startswith("session:sess-rohit:line:")
        assert "@rohit_dev_42" in json.dumps(payload)
        assert any(record["source"] == "fact" for record in payload["results"])
        assert any(record["source"] == "conversation" for record in payload["results"])
    finally:
        executor.close()
        conversations.close()
        memory.close()


def test_agent_continuation_searches_persisted_session_archive(tmp_path, fake_embedding_provider):
    data_dir = tmp_path / "data"
    database = data_dir / "ares.db"
    memory = MemoryStore(db_path=database, embedding_provider=fake_embedding_provider)
    sessions = SessionStore(data_dir)
    sessions.write_message("sess-old", "user", "Rohit Verma is my cousin.")
    sessions.write_message("sess-old", "assistant", "His Instagram ID is @rohit_dev_42.")
    agent = Agent(memory, config=AppConfig(data_dir=str(data_dir)), session_store=sessions, session_id="sess-current")
    try:
        context = agent.get_context("Continue: what was Rohit's Instagram ID from that session?")
        assert "@rohit_dev_42" in context
        assert "session:sess-old:line:" in context
    finally:
        agent.tool_executor.close()
        memory.close()


def test_plain_continue_keeps_recent_recall_when_an_older_turn_matches_the_word(tmp_path, fake_embedding_provider):
    data_dir = tmp_path / "data"
    database = data_dir / "ares.db"
    memory = MemoryStore(db_path=database, embedding_provider=fake_embedding_provider)
    conversations = ConversationStore(database)
    agent = Agent(memory, conversation_store=conversations, config=AppConfig(data_dir=str(data_dir)))
    try:
        conversation_id = conversations.start_conversation()
        conversations.add_message(conversation_id, "user", "Continue the old invoice discussion.")
        conversations.add_message(conversation_id, "user", "The current task is the Aurora release checklist.")

        context = agent.get_context("continue")

        assert "old invoice" in context
        assert "Aurora release checklist" in context
    finally:
        agent.tool_executor.close()
        conversations.close()
        memory.close()


def test_task_tool_output_hides_sensitive_step_arguments(tmp_path):
    handlers = TaskToolHandlers(TaskStore(tmp_path))
    secret = "private SMS message must not be echoed"
    output = handlers.create_task({
        "goal": "Message a saved person",
        "plan": [{"tool_name": "phone_send_sms", "arguments": {"number": "mom", "message": secret}}],
    })
    payload = json.loads(output)
    assert payload["ok"] is True
    assert secret not in output
    assert payload["task"]["plan"][0]["tool_name"] == "phone_send_sms"


def test_task_store_enforces_revisions_and_recovers_expired_leases(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create_task("Read a file", [{"tool_name": "read_file", "arguments": {"path": "note.txt"}}])
    updated = store.update_task(task["task_id"], goal="Read the current file", expected_revision=task["revision"])
    with pytest.raises(TaskConflictError):
        store.update_task(task["task_id"], goal="stale", expected_revision=task["revision"])

    claimed = store.claim_task(task["task_id"], lease_seconds=30)
    future = datetime.now(timezone.utc) + timedelta(minutes=2)
    recovered = store.recover_expired_leases(future)
    assert recovered and recovered[0]["task_id"] == task["task_id"]
    after = store.get_task(task["task_id"])
    assert after["status"] == "failed"
    assert after["revision"] > claimed["revision"] > updated["revision"]


def test_workflow_runner_records_every_step_and_requires_consolidated_confirmation(tmp_path):
    store = TaskStore(tmp_path)
    ledger = ActionLedger(tmp_path / "ares.db")
    calls: list[tuple[str, dict]] = []

    async def execute(tool_name: str, args: dict) -> str:
        calls.append((tool_name, dict(args)))
        return "Deleted file." if tool_name == "delete_file" else "Read file."

    task = store.create_task(
        "Inspect then remove a temporary file",
        [
            {"tool_name": "read_file", "arguments": {"path": "draft.txt"}},
            {"tool_name": "delete_file", "arguments": {"path": "draft.txt"}},
        ],
    )
    runner = AutonomousWorkflowRunner(task_store=store, action_ledger=ledger, execute_tool=execute)
    try:
        paused = json.loads(asyncio.run(runner.run(task["task_id"])))
        assert paused["ok"] is False
        assert paused["task"]["status"] == "awaiting_confirmation"
        assert [name for name, _args in calls] == ["read_file"]

        completed = json.loads(asyncio.run(runner.run(task["task_id"], confirm=True)))
        assert completed["ok"] is True
        assert completed["task"]["status"] == "completed"
        assert calls[-1] == ("delete_file", {"path": "draft.txt", "confirm": True})
        task_actions = ledger.search(task_id=task["task_id"], limit=50)
        assert any(record["action_type"] == "workflow_step_completed" for record in task_actions)
        assert any(record["action_type"] == "workflow_completed" for record in task_actions)
    finally:
        ledger.close()


def test_windows_workflow_action_requires_fresh_verify_before_marking_complete(tmp_path):
    store = TaskStore(tmp_path)
    ledger = ActionLedger(tmp_path / "ares.db")
    calls: list[str] = []

    async def execute(tool_name: str, args: dict) -> str:
        calls.append(tool_name)
        return "ok"

    task = store.create_task(
        "Click a desktop control",
        [{"tool_name": "mcp__windows__Click", "arguments": {"element": "Save"}}],
    )
    runner = AutonomousWorkflowRunner(task_store=store, action_ledger=ledger, execute_tool=execute)
    try:
        asyncio.run(runner.run(task["task_id"]))
        failed = json.loads(asyncio.run(runner.run(task["task_id"], confirm=True)))
        assert failed["ok"] is False
        assert failed["task"]["status"] == "failed"
        assert failed["task"]["current_step"] == 0
        assert calls == ["mcp__windows__Click"]

        verified_task = store.create_task(
            "Click and verify a desktop control",
            [{
                "tool_name": "mcp__windows__Click",
                "arguments": {"element": "Save"},
                "verify": {"tool_name": "mcp__windows__Snapshot", "arguments": {}, "contains": "saved"},
            }],
        )

        async def verified_execute(tool_name: str, args: dict) -> str:
            return "Document saved" if tool_name.endswith("Snapshot") else "ok"

        verified_runner = AutonomousWorkflowRunner(task_store=store, action_ledger=ledger, execute_tool=verified_execute)
        asyncio.run(verified_runner.run(verified_task["task_id"]))
        verified = json.loads(asyncio.run(verified_runner.run(verified_task["task_id"], confirm=True)))
        assert verified["ok"] is True
        assert verified["task"]["status"] == "completed"
    finally:
        ledger.close()


def test_agent_resolves_gmail_alias_before_mcp_without_leaking_email_to_ledger(tmp_path, fake_embedding_provider):
    class FakeMCP:
        tool_definitions: list[dict] = []

        def __init__(self):
            self.calls: list[tuple[str, dict]] = []

        async def call_tool(self, name: str, args: dict) -> str:
            self.calls.append((name, dict(args)))
            return json.dumps({"status": "sent", "id": "message-1"})

    data_dir = tmp_path / "data"
    memory = MemoryStore(db_path=data_dir / "ares.db", embedding_provider=fake_embedding_provider)
    mcp = FakeMCP()
    agent = Agent(memory, config=AppConfig(data_dir=str(data_dir)), mcp_manager=mcp)
    try:
        agent.people_store.create("Uma Rao", aliases=["uma"], email="uma@example.test")
        call = {
            "id": "gmail-1",
            "type": "function",
            "function": {
                "name": "mcp__google__gmail_send",
                "arguments": json.dumps({"to": "uma", "subject": "Hello", "body": "private email body"}),
            },
        }
        with agent.turn_scope(build_turn_execution_context("Send Uma this email")):
            results = asyncio.run(agent.process_tool_calls_async([call]))
        assert mcp.calls[0][1]["to"] == "uma@example.test"
        assert "uma@example.test" not in results[0]["content"]
        action = next(item for item in agent.action_ledger.list_all() if item["action_type"] == "email_sent")
        assert action["target"] == "uma"
        assert "uma@example.test" not in json.dumps(action)
        assert "private email body" not in json.dumps(action)
        contact = agent.people_store.resolve("uma", require="email")
        assert contact["last_contacted_via"] == "email"
        assert contact["last_contacted_at"]

        calendar_call = {
            "id": "calendar-1",
            "type": "function",
            "function": {
                "name": "mcp__google__calendar_create_event",
                "arguments": json.dumps({
                    "summary": "Private meeting", "start_time": "2026-07-12T10:00:00Z",
                    "end_time": "2026-07-12T10:30:00Z", "attendees": ["uma"],
                }),
            },
        }
        with agent.turn_scope(build_turn_execution_context("Create this calendar event and invite Uma")):
            asyncio.run(agent.process_tool_calls_async([calendar_call]))
        assert mcp.calls[1][1]["attendees"] == ["uma@example.test"]
        calendar_action = next(item for item in agent.action_ledger.list_all() if item["action_type"] == "calendar_event_created")
        assert "uma@example.test" not in json.dumps(calendar_action)
    finally:
        agent.tool_executor.close()
        memory.close()


def test_agent_adds_playwright_stale_ref_recovery_without_blind_retry(tmp_path, fake_embedding_provider):
    class FakeMCP:
        tool_definitions: list[dict] = []

        def __init__(self):
            self.calls = 0

        async def call_tool(self, name: str, _args: dict) -> str:
            self.calls += 1
            if name.endswith("browser_snapshot"):
                return "Fresh browser snapshot with ref e22."
            return "Error: reference e17 is stale and does not exist."

    data_dir = tmp_path / "data"
    memory = MemoryStore(db_path=data_dir / "ares.db", embedding_provider=fake_embedding_provider)
    mcp = FakeMCP()
    agent = Agent(memory, config=AppConfig(data_dir=str(data_dir)), mcp_manager=mcp)
    try:
        # Seed a valid observation. The controller correctly refuses blind
        # interactions when no fresh snapshot exists.
        agent.browser_controller.after_call(
            agent.session_id,
            "mcp__playwright__browser_snapshot",
            {},
            "Initial browser snapshot with ref e17.",
        )
        call = {
            "id": "playwright-1",
            "type": "function",
            "function": {
                "name": "mcp__playwright__browser_click",
                "arguments": json.dumps({"ref": "e17"}),
            },
        }
        with agent.turn_scope(build_turn_execution_context("Click the referenced button in the browser")):
            result = asyncio.run(agent.process_tool_calls_async([call]))[0]["content"]
        assert mcp.calls == 2
        assert "Do not retry the old ref" in result
        assert "Fresh browser snapshot" in result
    finally:
        agent.tool_executor.close()
        memory.close()


def test_agent_runs_real_local_workflow_and_links_ledger_to_task(tmp_path, fake_embedding_provider):
    data_dir = tmp_path / "data"
    target = tmp_path / "draft.txt"
    target.write_text("draft", encoding="utf-8")
    memory = MemoryStore(db_path=data_dir / "ares.db", embedding_provider=fake_embedding_provider)
    agent = Agent(memory, config=AppConfig(data_dir=str(data_dir)))
    try:
        created = json.loads(agent.tool_executor.execute("create_task", {
            "goal": "Polish a local draft",
            "plan": [{
                "tool_name": "edit_file",
                "arguments": {"path": str(target), "old_text": "draft", "new_text": "final"},
                "description": "Replace the draft marker.",
            }],
        }))
        task_id = created["task"]["task_id"]
        call = {
            "id": "run-local-task",
            "type": "function",
            "function": {"name": "run_task", "arguments": json.dumps({"task_id": task_id})},
        }
        with agent.turn_scope(build_turn_execution_context("Run the saved task")):
            result = asyncio.run(agent.process_tool_calls_async([call]))
        payload = json.loads(result[0]["content"])
        assert payload["ok"] is True and payload["task"]["status"] == "completed"
        assert target.read_text(encoding="utf-8") == "final"
        linked = agent.action_ledger.search(task_id=task_id, limit=20)
        assert any(action["action_type"] == "file_edited" for action in linked)
        assert any(action["action_type"] == "workflow_step_completed" for action in linked)
    finally:
        agent.tool_executor.close()
        memory.close()
