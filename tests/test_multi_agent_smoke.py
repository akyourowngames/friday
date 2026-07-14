from __future__ import annotations

import asyncio
import inspect

import ares.multi_agent_smoke as smoke_module
from ares.multi_agent_smoke import main, run_smoke_scenarios


def test_production_path_smoke_scenarios_cover_a_through_e() -> None:
    results = asyncio.run(run_smoke_scenarios())

    assert tuple(result.scenario for result in results) == ("A", "B", "C", "D", "E")
    by_scenario = {result.scenario: result for result in results}

    scenario_a = by_scenario["A"]
    assert scenario_a.evidence["entrypoint"] == "Agent.run_stream"
    assert scenario_a.evidence["runtime"] == "MultiAgentRuntime"
    assert scenario_a.evidence["advertised_tools"] == [[], []]
    assert scenario_a.evidence["executor_calls"] == ()
    assert scenario_a.evidence["run_count"] == 0

    scenario_b = by_scenario["B"]
    manifest_b = scenario_b.evidence["manifest"]
    assert scenario_b.evidence["entrypoint"] == "Agent.run_stream -> MultiAgentRuntime.delegate"
    assert scenario_b.evidence["root_run_id"].startswith("ma_")
    assert manifest_b["root_run_id"] == scenario_b.evidence["root_run_id"]
    assert manifest_b["agent_count"] == 4
    assert manifest_b["execution_waves"] == [
        ["researcher_1", "researcher_2", "researcher_3"],
        ["synthesis"],
    ]
    assert [child["role"] for child in manifest_b["child_runs"]] == [
        "researcher",
        "researcher",
        "researcher",
        "synthesizer",
    ]
    assert len({child["run_id"] for child in manifest_b["child_runs"]}) == 4
    assert scenario_b.evidence["peak_researchers"] == 3
    assert scenario_b.evidence["store_round_trip"] is True

    scenario_c = by_scenario["C"]
    assert scenario_c.evidence["entrypoint"] == "Agent.run_stream meta route"
    assert scenario_c.evidence["root_run_id"] == scenario_b.evidence["root_run_id"]
    assert scenario_c.evidence["llm_calls"] == 0
    assert scenario_c.evidence["new_runs"] == 0
    assert scenario_c.evidence["cross_session_visible"] is False

    scenario_d = by_scenario["D"]
    assert scenario_d.evidence["entrypoint"] == "Agent.run_stream"
    assert scenario_d.evidence["runtime"] is None
    assert scenario_d.evidence["llm_calls"] == 0
    assert "no agents ran" in scenario_d.evidence["answer"].casefold()

    scenario_e = by_scenario["E"]
    manifest_e = scenario_e.evidence["manifest"]
    assert scenario_e.evidence["entrypoint"].startswith("Agent.process_tool_calls_async")
    assert scenario_e.evidence["root_run_id"].startswith("ma_")
    assert manifest_e["agent_count"] == 3
    assert manifest_e["execution_waves"] == [["builder_1", "builder_2"], ["review"]]
    assert scenario_e.evidence["peak_mutations"] == 1
    assert scenario_e.evidence["reviewer_verified"] is True
    assert sorted(scenario_e.evidence["final_lines"][1:]) == ["builder_1", "builder_2"]
    assert set(scenario_e.evidence["workspace_records"]) == {"builder_1", "builder_2"}


def test_smoke_harness_never_constructs_orchestrator_manifests_itself() -> None:
    source = inspect.getsource(smoke_module)

    assert "_manifest_from_team" not in source
    assert "AgentExecutionManifest(" not in source
    assert "ChildRunManifest(" not in source
    assert "MultiAgentOrchestrator(" not in source


def test_module_main_reports_stable_output_and_success(capsys) -> None:
    assert main() == 0

    assert capsys.readouterr().out.splitlines() == [
        "[PASS] Scenario A: real Agent greeting; tools=0; stale execution=0; native runs=0",
        "[PASS] Scenario B: real native runtime; generated root; agents=4; waves=3+1; sources=3; workflow substitutions=0",
        "[PASS] Scenario C: real Agent meta route; persisted exact agents=4; model calls=0; new runs=0",
        "[PASS] Scenario D: real Agent disabled path; claimed agents=0; model fallback=0; runtime=absent",
        "[PASS] Scenario E: real Agent/runtime delegation; same-path builders serialized; peak mutations=1; reviewer=verified; lost writes=0",
        "[PASS] production-path multi-agent smoke: 5/5 scenarios",
    ]
