# Friday CLI

Fast Python CLI assistant powered by NVIDIA's OpenAI-compatible NIM endpoint.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python chat.py --ping
python chat.py
```

The API key is read from `.env`. The default chat model is:

```env
NVIDIA_MODEL=mistralai/ministral-14b-instruct-2512
PERSONA_FILE=persona.md
```

Useful commands inside the CLI:

```text
/help
/model
/memory
/memory rebuild
/memory files
/memory search <query>
/remember <project|user|preferences|personal> <fact>
/tools
/tool weather location=Delhi
/tool realtime_search query="latest NVIDIA NIM models" max_results=5
/voice
/voice on
/voice off
/clear
/ping
/exit
```

Streaming is enabled through `POST /v1/chat/completions` using NVIDIA's OpenAI-compatible API.

## Memory

Friday uses LangChain JSONL chat history plus LlamaIndex for local memory retrieval over these editable text files:

```text
memory/project.txt
memory/user.txt
memory/preferences.txt
memory/personal.txt
```

The retriever uses NVIDIA embeddings:

```env
NVIDIA_EMBED_MODEL=nvidia/nv-embedqa-e5-v5
```

Private memory files and the generated `.memory_index/` vector index are ignored by git.
Each session is also saved to `sessions/<session-id>.jsonl`, and the last 20 messages are sent to the model.
`AUTO_LLM_MEMORY=true` runs a prompt-driven fact extraction pass after each assistant reply so durable user/project/preferences facts can persist without local phrase shortcuts.

## Tools

Friday has a registry-driven local tool layer. Slash commands are explicit and fast; normal conversation still goes through the assistant model.
Each tool lives in its own module under `assistant_cli/tools/<tool_name>.py`.
The versioned catalog lives in `memory/registry/tools.md`.

```powershell
python chat.py --tool calculator --tool-args 'expression="(22/7)*3"'
python chat.py --tool weather --tool-args 'location=Delhi'
```

Inside chat:

```text
/tools
/tool geocode location="New Delhi"
/tool unit_convert value=72 from_unit=fahrenheit to_unit=celsius
```

Realtime search uses Tavily when you add the key:

```env
TAVILY_API_KEY=your_tavily_key_here
FRIDAY_TOOLS_ENABLED=true
FRIDAY_AUTO_TOOLS_ENABLED=true
FRIDAY_TOOL_TIMEOUT_SECONDS=8
FRIDAY_TOOL_PLANNER_TIMEOUT_SECONDS=8
FRIDAY_TOOL_MIN_CONFIDENCE=0.45
```

Registered tools include realtime search, weather, geocode, reverse geocode, current time, calculator, unit conversion, hashing, base64 encode/decode, JSON formatting, UUIDs, random numbers, password generation, URL fetch, workspace file list/read, and local notes.
See `memory/registry/tools.md` for one-line CLI prompts for every tool.
Normal chat uses a model-planned router over the registered tool specs, then streams a natural model-written answer from the tool result.

## Persona

`persona.md` is loaded as Friday's system prompt for every chat request. Edit that file to tune the assistant voice, identity behavior, and operating rules.

## Voice

Normal mode stays text-only:

```powershell
python chat.py
```

Voice mode enables Sarvam TTS plus Ctrl+Space voice input:

```powershell
python chat.py --voice
```

In voice mode, press Ctrl+Space to start recording, then press Ctrl+Space again to transcribe and send. Normal Space stays untouched for typing. Voice checks:

```powershell
python chat.py --voice-test
python chat.py --voice-roundtrip-test
python chat.py --transcribe-test storage/voice/some-file.wav
```
