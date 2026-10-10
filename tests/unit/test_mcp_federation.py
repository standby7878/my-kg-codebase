import pytest

from codekg.mcp.server import mcp


def test_federated_response_limit_drops_every_oversized_collection():
    import json

    from codekg.mcp.server import _federated_result

    for key in ("items", "candidates", "graphs", "changes", "paths", "usages"):
        result = _federated_result("probe", {"status": "ok", key: ["x" * 70000]})
        assert result.structured_content["status"] == "result_too_large"
        assert result.structured_content["truncated"] is True
        assert len(json.dumps(result.structured_content).encode()) < 65536


@pytest.mark.asyncio
async def test_federation_tools_return_typed_context_errors_without_falling_back(monkeypatch):
    monkeypatch.delenv("CODEKG_GRAPH_REGISTRY", raising=False)
    tool = (await mcp.get_tools())["resolve_database_intent"]
    result = await tool.run(
        {
            "evidence_ref": {"graph_id": "app", "generation_id": "g1", "local_key": "e1"},
            "context_id": "missing-context",
        }
    )
    assert result.structured_content["status"] == "invalid_graph_or_context"
    assert "graph registry is not configured" in result.structured_content["message"]


@pytest.mark.asyncio
async def test_graph_discovery_reports_missing_registry_as_typed_status(monkeypatch):
    monkeypatch.delenv("CODEKG_GRAPH_REGISTRY", raising=False)
    tool = (await mcp.get_tools())["list_knowledge_graphs"]
    result = await tool.run({})
    assert result.structured_content["status"] == "registry_error"


@pytest.mark.asyncio
async def test_intent_continuation_is_bound_to_application_generation(monkeypatch):
    from types import SimpleNamespace

    import codekg.mcp.server as server

    monkeypatch.setenv("CODEKG_GRAPH_REGISTRY", "/fixture/registry.toml")
    selected = {"handle": SimpleNamespace(graph_id="app", generation_id="app:g1")}
    monkeypatch.setattr(
        server.graph_federation, "handle", lambda _graph_id=None: selected["handle"]
    )
    monkeypatch.setattr(
        server.graph_federation,
        "list_database_intents",
        lambda **_kwargs: {
            "status": "ok",
            "graph_id": "app",
            "generation_id": selected["handle"].generation_id,
            "items": [],
            "truncated": True,
            "continuation": "evidence-key-2",
        },
    )
    tool = (await mcp.get_tools())["list_database_intents"]
    first = await tool.run({"owner_path": "src/a.py", "graph_id": "app"})
    cursor = first.structured_content["continuation"]
    assert cursor.startswith("ck1.")

    selected["handle"] = SimpleNamespace(graph_id="app", generation_id="app:g2")
    rejected = await tool.run({"owner_path": "src/a.py", "graph_id": "app", "after_key": cursor})
    assert rejected.structured_content["status"] == "invalid_graph_or_selector"
