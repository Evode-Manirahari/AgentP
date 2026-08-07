import importlib

import pytest


def test_fastapi_app_imports_when_runtime_dependencies_are_available() -> None:
    for module_name in [
        "fastapi",
        "httpx",
        "mcp",
        "psycopg",
        "pydantic_settings",
        "redis",
        "rq",
        "sqlalchemy",
    ]:
        pytest.importorskip(module_name)

    module = importlib.import_module("app.main")

    assert module.app.title == "AgentP Document Execution API"
