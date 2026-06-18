"""Pydantic data models for Ares."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class FactCategory(str, Enum):
    PREFERENCE = "preference"
    FACT = "fact"
    BELIEF = "belief"
    HABIT = "habit"
    RELATIONSHIP = "relationship"
    NOTE = "note"


class TaskPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TaskStatus(str, Enum):
    PENDING = "pending"
    DONE = "done"
    CANCELLED = "cancelled"


class Memory(BaseModel):
    fact_id: Optional[int] = None
    fact_text: str
    category: FactCategory = FactCategory.NOTE
    confidence: float = 1.0
    importance: float = 0.5
    source: str = "conversation"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_accessed: Optional[str] = None
    access_count: int = 0
    superseded_by: Optional[int] = None


class Task(BaseModel):
    id: Optional[int] = None
    title: str
    description: Optional[str] = None
    due: Optional[str] = None
    priority: TaskPriority = TaskPriority.MEDIUM
    status: TaskStatus = TaskStatus.PENDING
    created_at: Optional[str] = None
    completed_at: Optional[str] = None
    reminder_at: Optional[str] = None
    reminder_sent_at: Optional[str] = None


class ConversationMessage(BaseModel):
    id: Optional[int] = None
    conversation_id: Optional[int] = None
    role: str
    content: str
    tool_call_id: Optional[str] = None
    tool_calls: Optional[list[dict]] = None
    timestamp: Optional[str] = None


class AppConfig(BaseModel):
    model: str = "deepseek-v4-flash-free"
    api_key: str = ""
    api_base_url: str = "https://opencode.ai/zen/v1"
    max_context_messages: int = 20
    max_memory_retrieval: int = 5
    data_dir: str = "~/.ares/data"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_backend: str = "onnx"
    embedding_provider: str = "CPUExecutionProvider"
    embedding_file_name: str = ""
    reminder_poll_seconds: int = 30
    enable_desktop_notifications: bool = True
    session_summary_messages: int = 2
    web_search_provider: str = "auto"
    tavily_api_key: str = ""
    tavily_search_depth: str = "basic"
