from ares.skills.commitments import CommitmentStore
from ares.context.discovery import ProjectContext
from ares.context.conversations import ConversationStore
from ares.skills.goals import GoalStore
from ares.memory import MemoryStore
from ares.models import AppConfig
from ares.profile import ProfileManager
from ares.soul import SoulManager
from ares.context.user_context import build_user_context, is_deep_context_request


def test_build_user_context_retrieves_all_durable_layers(tmp_path, fake_embedding_provider):
    database = tmp_path / "ares.db"
    memory = MemoryStore(database, embedding_provider=fake_embedding_provider)
    conversations = ConversationStore(database)
    goals = GoalStore(database, connection=memory.conn)
    commitments = CommitmentStore(database, connection=memory.conn)
    profile = ProfileManager(tmp_path)
    soul = SoulManager(tmp_path)
    profile.ensure_exists()
    soul.ensure_exists()
    profile.write(profile.read().replace("- Name:", "- Name: Riya"))
    memory.store("User prefers concise release notes", category="preference")
    goal = goals.create("Ship context retrieval", next_action="Run context tests")
    commitments.create("Review the context output", owner="user")
    previous = conversations.start_conversation()
    conversations.add_exchange(previous, "The codename is Orion", "I will remember it.")
    conversations.end_conversation(previous)
    current = conversations.start_conversation()

    context = build_user_context(
        "What is left for the release notes?",
        config=AppConfig(
            data_dir=str(tmp_path),
            project_context_enabled=False,
            model="deepseek-v4-flash-free",
        ),
        soul_manager=soul,
        profile_manager=profile,
        project_context=ProjectContext(enabled=False),
        memory_store=memory,
        conversation_store=conversations,
        session_id=f"conversation-{current}",
        goal_store=goals,
        commitment_store=commitments,
        conversation_history=[],
    )

    assert "Riya" in context
    assert "concise release notes" in context
    assert "Ship context retrieval" in context
    assert "Run context tests" in context
    assert "Review the context output" in context
    assert "The codename is Orion" in context
    assert f"#{goal['goal_id']}" in context
    conversations.close()
    memory.close()


def test_fast_context_avoids_unnecessary_durable_store_calls():
    calls: list[str] = []

    class StaticContext:
        def get_context(self, **_kwargs):
            calls.append("static")
            return ""

    class Memory:
        def search(self, _query, **_kwargs):
            calls.append("memory.search")
            return []

    class People:
        def mentioned_in(self, _query, **_kwargs):
            calls.append("people.mentioned")
            return []

        def recent_for_context(self, **_kwargs):
            calls.append("people.recent")
            return []

    class Actions:
        def recent(self, **_kwargs):
            calls.append("actions.recent")
            return []

        def search(self, *_args, **_kwargs):
            calls.append("actions.search")
            return []

    class Goals:
        def list_all(self, **_kwargs):
            calls.append("goals.list")
            return []

        def search(self, *_args, **_kwargs):
            calls.append("goals.search")
            return []

        def contextualize_goals(self, goals, **_kwargs):
            calls.append("goals.contextualize")
            return goals

        def due_soon(self, **_kwargs):
            calls.append("goals.due")
            return []

        def overdue(self):
            calls.append("goals.overdue")
            return []

        def mark_watcher_signals_surfaced(self, _ids):
            calls.append("goals.mark")

    class Tasks:
        def list_tasks(self, **_kwargs):
            calls.append("tasks.list")
            return []

    class Commitments:
        def list_pending(self, **_kwargs):
            calls.append("commitments.list")
            return []

    class FollowUps:
        def list_open(self, **_kwargs):
            calls.append("followups.list")
            return []

    class Conversations:
        def get_recent_context_messages(self, **_kwargs):
            calls.append("conversations.recent")
            return []

        def search_recall(self, *_args, **_kwargs):
            calls.append("conversations.search")
            return []

        def get_recent_summaries(self, **_kwargs):
            calls.append("conversations.summaries")
            return []

    class Sessions:
        def search_recall(self, *_args, **_kwargs):
            calls.append("sessions.search")
            return []

        def get_previous_summary(self, *_args, **_kwargs):
            calls.append("sessions.summary")
            return None

    build_user_context(
        "Hello there",
        config=AppConfig(model="deepseek-v4-flash-free", project_context_enabled=False),
        soul_manager=StaticContext(),
        profile_manager=StaticContext(),
        project_context=StaticContext(),
        memory_store=Memory(),
        conversation_store=Conversations(),
        session_store=Sessions(),
        session_id="conversation-1",
        people_store=People(),
        action_ledger=Actions(),
        task_store=Tasks(),
        goal_store=Goals(),
        commitment_store=Commitments(),
        follow_up_store=FollowUps(),
        conversation_history=[],
    )

    assert calls.count("static") == 2
    assert "memory.search" in calls
    assert not {
        "people.mentioned", "people.recent", "actions.recent", "actions.search",
        "goals.list", "goals.search", "goals.contextualize", "goals.due", "goals.overdue",
        "tasks.list", "commitments.list", "followups.list", "conversations.recent",
        "conversations.search", "conversations.summaries", "sessions.search", "sessions.summary",
    }.intersection(calls)


def test_deep_context_request_retrieves_cross_session_and_proactive_layers():
    calls: list[str] = []

    class StaticContext:
        def get_context(self, **_kwargs):
            return ""

    class Memory:
        def search(self, _query, **_kwargs):
            return []

    class People:
        def mentioned_in(self, _query, **_kwargs):
            calls.append("people.mentioned")
            return []

        def recent_for_context(self, **_kwargs):
            calls.append("people.recent")
            return []

    class Actions:
        def recent(self, **_kwargs):
            calls.append("actions.recent")
            return []

        def search(self, *_args, **_kwargs):
            calls.append("actions.search")
            return []

    class Goals:
        def list_all(self, **_kwargs):
            calls.append("goals.list")
            return [{"goal_id": 1, "title": "Ship it", "status": "active"}]

        def contextualize_goals(self, goals, **_kwargs):
            calls.append("goals.contextualize")
            return goals

        def due_soon(self, **_kwargs):
            calls.append("goals.due")
            return []

        def overdue(self):
            calls.append("goals.overdue")
            return []

        def mark_watcher_signals_surfaced(self, _ids):
            calls.append("goals.mark")

    class Tasks:
        def list_tasks(self, **_kwargs):
            calls.append("tasks.list")
            return []

    class Commitments:
        def list_pending(self, **_kwargs):
            calls.append("commitments.list")
            return []

    class FollowUps:
        def list_open(self, **_kwargs):
            calls.append("followups.list")
            return []

    class Conversations:
        def get_recent_context_messages(self, **_kwargs):
            calls.append("conversations.recent")
            return []

        def search_recall(self, *_args, **_kwargs):
            calls.append("conversations.search")
            return []

        def get_recent_summaries(self, **_kwargs):
            calls.append("conversations.summaries")
            return []

    class Sessions:
        def search_recall(self, *_args, **_kwargs):
            calls.append("sessions.search")
            return []

        def get_previous_summary(self, *_args, **_kwargs):
            calls.append("sessions.summary")
            return None

    request = "What did I say previously about my pending tasks?"
    assert is_deep_context_request(request)
    build_user_context(
        request,
        config=AppConfig(model="deepseek-v4-flash-free", project_context_enabled=False),
        soul_manager=StaticContext(),
        profile_manager=StaticContext(),
        project_context=StaticContext(),
        memory_store=Memory(),
        conversation_store=Conversations(),
        session_store=Sessions(),
        session_id="conversation-1",
        people_store=People(),
        action_ledger=Actions(),
        task_store=Tasks(),
        goal_store=Goals(),
        commitment_store=Commitments(),
        follow_up_store=FollowUps(),
        conversation_history=[],
    )

    assert {
        "people.mentioned", "people.recent", "actions.recent", "actions.search", "goals.list",
        "goals.contextualize", "goals.due", "goals.overdue", "tasks.list", "commitments.list",
        "followups.list", "conversations.recent", "sessions.search", "sessions.summary",
    }.issubset(calls)
    # A live session uses its JSONL recall path; SQL conversation recall and
    # archived summaries remain reserved for non-session conversations.
    assert "conversations.search" not in calls
    assert "conversations.summaries" not in calls
