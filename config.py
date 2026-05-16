import os
from pathlib import Path


def _load_env():
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())


_load_env()


class Settings:
    nim_api_key: str = os.getenv("NVIDIA_API_KEY", "")
    nim_base_url: str = "https://integrate.api.nvidia.com/v1"
    model_name: str = "meta/llama-3.1-8b-instruct"
    debug: bool = False
    tool_top_k: int = 8
    tool_similarity_threshold: float = 0.25
    max_tool_rounds: int = 6
    embedding_model: str = "nvidia/nv-embed-v1"
    memory_dir: str = "storage/memories"
    sarvam_api_key: str = os.getenv("SARVAM_API_KEY", "")
    tavily_api_key: str = os.getenv("TAVILY_API_KEY", "")
    voice_enabled: bool = False
    voice_language: str = "en-IN"


settings = Settings()
