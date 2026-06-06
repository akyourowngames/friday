from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from assistant_cli.config import Settings
from assistant_cli.tools import TOOL_MODULES, build_default_registry


def make_settings(root: Path, tavily_api_key: str = "") -> Settings:
    return Settings(
        api_key="test-nvidia-key",
        base_url="https://integrate.api.nvidia.com/v1",
        model="test-model",
        embed_model="test-embed",
        persona_file="persona.md",
        temperature=0.0,
        max_tokens=200,
        memory_dir=str(root / "memory"),
        memory_index_dir=str(root / ".memory_index"),
        memory_top_k=4,
        session_dir=str(root / "sessions"),
        last_messages=20,
        auto_llm_memory=False,
        auto_llm_memory_async=True,
        sarvam_api_key="",
        voice_enabled=False,
        voice_speaker="priya",
        voice_language="en-IN",
        voice_model="bulbul:v3",
        voice_output_dir=str(root / "storage" / "voice"),
        voice_sample_rate=24000,
        voice_codec="wav",
        voice_pace=1.15,
        voice_temperature=0.55,
        voice_max_chars=900,
        voice_input_enabled=False,
        voice_hotkey="ctrl+space",
        voice_hold_seconds=0.3,
        stt_model="saaras:v3",
        stt_mode="transcribe",
        stt_language="en-IN",
        stt_sample_rate=16000,
        stt_max_seconds=30.0,
        stt_min_seconds=0.35,
        stt_output_dir=str(root / "storage" / "voice_input"),
        tavily_api_key=tavily_api_key,
        tools_enabled=True,
        auto_tools_enabled=True,
        tool_timeout_seconds=1.0,
        tool_planner_model="test-model",
        tool_planner_timeout_seconds=1.0,
        tool_min_confidence=0.45,
        tool_prefilter_threshold=0.10,
        tool_prefilter_max_candidates=5,
        tool_result_max_chars=6000,
        debug_timing=False,
    )


class ToolRegistryTests(unittest.TestCase):
    def build_registry(self, root: Path, client: httpx.Client | None = None, tavily_api_key: str = ""):
        return build_default_registry(make_settings(root, tavily_api_key=tavily_api_key), root, client)

    def test_registry_has_expected_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = self.build_registry(Path(tmp))
            names = set(registry.names())
        self.assertGreaterEqual(len(names), 13)
        for name in {"realtime_search", "weather", "geocode", "calculator", "unit_convert", "file_read"}:
            self.assertIn(name, names)

    def test_each_registered_tool_has_own_module(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.assertFalse((root / "assistant_cli" / "tools.py").exists())

        with tempfile.TemporaryDirectory() as tmp:
            registry = self.build_registry(Path(tmp))
            names = set(registry.names())

        modules_by_tool = {module.SPEC.name: module.__name__.rsplit(".", 1)[-1] for module in TOOL_MODULES}
        self.assertEqual(names, set(modules_by_tool))
        for tool_name, module_name in modules_by_tool.items():
            self.assertEqual(tool_name, module_name)
            self.assertTrue((root / "assistant_cli" / "tools" / f"{module_name}.py").exists())

    def test_local_utility_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = self.build_registry(Path(tmp))
            calc = registry.execute("calculator", {"expression": "(22 / 7) * 3"})
            temp = registry.execute("unit_convert", {"value": 72, "from_unit": "fahrenheit", "to_unit": "celsius"})
            encoded = registry.execute("base64_encode", {"text": "friday"})
            decoded = registry.execute("base64_decode", {"text": encoded.text})
            pretty = registry.execute("json_format", {"json_text": '{"b":2,"a":1}'})

        self.assertTrue(calc.ok, calc.text)
        self.assertIn("9.428", calc.text)
        self.assertTrue(temp.ok, temp.text)
        self.assertAlmostEqual(temp.data["result"], 22.2222222222)
        self.assertEqual(decoded.text, "friday")
        self.assertEqual(json.loads(pretty.text), {"a": 1, "b": 2})

    def test_file_tools_stay_inside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "safe.txt").write_text("hello", encoding="utf-8")
            registry = self.build_registry(root)
            safe = registry.execute("file_read", {"path": "safe.txt"})
            escape = registry.execute("file_read", {"path": "..\\outside.txt"})

        self.assertTrue(safe.ok, safe.text)
        self.assertEqual(safe.text, "hello")
        self.assertFalse(escape.ok)
        self.assertIn("inside", escape.text)

    def test_tavily_missing_key_is_clean_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = self.build_registry(Path(tmp))
            result = registry.execute("realtime_search", {"query": "NVIDIA NIM", "max_results": 3})

        self.assertFalse(result.ok)
        self.assertIn("TAVILY_API_KEY", result.text)
        self.assertLess(result.latency_ms, 100)

    def test_weather_and_geocode_use_http_client(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "geocoding-api.open-meteo.com" in url:
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "name": "Delhi",
                                "admin1": "Delhi",
                                "country": "India",
                                "latitude": 28.6519,
                                "longitude": 77.2315,
                            }
                        ]
                    },
                )
            if "api.open-meteo.com" in url:
                return httpx.Response(
                    200,
                    json={
                        "current": {
                            "temperature_2m": 31.5,
                            "apparent_temperature": 33.0,
                            "relative_humidity_2m": 48,
                            "wind_speed_10m": 9.2,
                            "precipitation": 0,
                        },
                        "current_units": {
                            "temperature_2m": "C",
                            "apparent_temperature": "C",
                            "relative_humidity_2m": "%",
                            "wind_speed_10m": "km/h",
                            "precipitation": "mm",
                        },
                    },
                )
            return httpx.Response(404)

        with tempfile.TemporaryDirectory() as tmp:
            client = httpx.Client(transport=httpx.MockTransport(handler))
            registry = self.build_registry(Path(tmp), client)
            geocode = registry.execute("geocode", {"location": "Delhi"})
            weather = registry.execute("weather", {"location": "Delhi"})

        self.assertTrue(geocode.ok, geocode.text)
        self.assertIn("lat=28.6519", geocode.text)
        self.assertTrue(weather.ok, weather.text)
        self.assertIn("Temperature: 31.5 C", weather.text)

    def test_notes_are_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.build_registry(root)
            added = registry.execute("note_add", {"text": "tool test note", "tags": ["test"]})
            listed = registry.execute("note_list", {"limit": 5})
            notes_file = root / "storage" / "tool_notes.jsonl"

            self.assertTrue(added.ok, added.text)
            self.assertTrue(listed.ok, listed.text)
            self.assertTrue(notes_file.exists())
            self.assertIn("tool test note", listed.text)


if __name__ == "__main__":
    unittest.main()
