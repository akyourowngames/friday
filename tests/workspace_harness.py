"""Isolated live runtime used for rendered Ares workspace verification."""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from ares.conversations import ConversationStore
from ares.models import AppConfig
from ares.server import AresServer
from ares.skills import SkillManager
from ares.watcher.tools import WatcherToolHandlers


class HarnessServer(AresServer):
    async def _handle_load_session(self, websocket, message):
        await asyncio.sleep(0.35)
        await super()._handle_load_session(websocket, message)


class HarnessLLM:
    async def chat(self, messages, tools=None):
        return {"content": "Ares visual harness response."}


class HarnessMemory:
    def get_recent(self, limit=100): return []
    def list_all(self): return []
    def search(self, query, limit=5): return []
    def count(self): return 3
    def close(self): return None


class HarnessExecutor:
    def __init__(self, root: Path):
        self.watcher_tools = WatcherToolHandlers(root / "watchers.db")
        self.mcp_manager = None
        self._terminal_display_callback = None

    def set_watcher_service(self, service):
        self.watcher_tools.set_service(service)

    def close(self):
        self.watcher_tools.close()


class HarnessAgent:
    def __init__(self, config: AppConfig, root: Path):
        self.config = config
        self.model = config.model
        self.llm = HarnessLLM()
        self.tool_executor = HarnessExecutor(root)
        self.skill_manager = SkillManager([root / "skills"])
        self.mcp_manager = None
        self.root = root
        self._session = ContextVar("harness_session", default="none")

    @contextmanager
    def session_scope(self, session_id):
        token = self._session.set(session_id)
        try:
            yield
        finally:
            self._session.reset(token)

    async def run_stream(self, message, conversation_history=None):
        await asyncio.sleep(1.2 if "slow" in message.lower() else 0.45)
        if "artifact" in message.lower():
            artifact = self.root / "generated-demo.md"
            artifact.write_text("# Generated brief\n\n- Background chat stayed isolated.\n- **Markdown rendering** is active.\n\n| Check | Result |\n|---|---|\n| Routing | Passed |", encoding="utf-8")
            yield f'[tool:write_file:{{"output_path":{artifact.as_posix()!r}}}]'.replace("'", '"')
        for chunk in (
            "[tool_start:list_workspace_files]",
            '[tool:list_workspace_files:{"count":3}]',
            f"## {self._session.get()}\n\n",
            f"Harness answer for **{message}**.\n\n- Streaming is live\n- Chat context is isolated",
        ):
            await asyncio.sleep(0.2)
            yield chunk

    def get_context(self, query=""): return f"Harness context: {query}"
    def set_model(self, model): self.model = model
    def set_session_id(self, session_id): self.session_id = session_id
    def apply_config(self, config): self.config = config; self.model = config.model
    def set_mcp_manager(self, manager): self.mcp_manager = manager; self.tool_executor.mcp_manager = manager
    def refresh_tools(self): return None
    def close(self): self.tool_executor.close()


async def run(host: str, api_port: int, workspace_port: int) -> None:
    root = Path(tempfile.mkdtemp(prefix="ares-workspace-harness-"))
    conversations = ConversationStore(root / "conversations.db")
    for index in range(36):
        conversation_id = conversations.start_conversation()
        conversations.add_message(conversation_id, "user", f"Archive conversation {index + 1}")
        conversations.add_message(conversation_id, "assistant", "Archived response for sidebar overflow verification.")
    long_conversation_id = conversations.start_conversation()
    for index in range(45):
        conversations.add_message(long_conversation_id, "user", f"Long scroll checkpoint {index + 1}")
        conversations.add_message(long_conversation_id, "assistant", f"Verified checkpoint {index + 1}. The conversation remains fully scrollable.")
    searchable_id = conversations.start_conversation()
    conversations.add_message(searchable_id, "user", "Plan the product launch")
    conversations.add_message(searchable_id, "assistant", "The hidden nebula marker proves full-content search works.")
    config = AppConfig(
        onboarding_completed=True,
        data_dir=str(root),
        mcp_servers=[],
        watcher={"enabled": False},
        workspace={"enabled": True, "host": host, "port": workspace_port},
    )
    agent = HarnessAgent(config, root)
    server = HarnessServer(
        host=host,
        port=api_port,
        config=config,
        agent=agent,
        memory_store=HarnessMemory(),
        conversation_store=conversations,
        start_workspace=True,
        start_watcher_dashboard=False,
    )
    try:
        await server.run_forever()
    finally:
        await server.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--api-port", type=int, default=18765)
    parser.add_argument("--workspace-port", type=int, default=18766)
    args = parser.parse_args()
    asyncio.run(run(args.host, args.api_port, args.workspace_port))
