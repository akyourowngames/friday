from ares.commitments import CommitmentStore
from ares.context import ProjectContext
from ares.conversations import ConversationStore
from ares.goals import GoalStore
from ares.memory import MemoryStore
from ares.models import AppConfig
from ares.profile import ProfileManager
from ares.soul import SoulManager
from ares.user_context import build_user_context


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
