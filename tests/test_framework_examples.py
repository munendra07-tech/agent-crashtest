"""The framework examples in examples/frameworks/, run as agents under test.

Each example needs its framework installed in its own Python (requirements in
examples/frameworks/). Point these variables at that Python to run them; without
one, its test is skipped:
  CRASHTEST_EXAMPLE_PYDANTIC_AI_PYTHON  pydantic-ai-slim[mcp]==2.68.0
  CRASHTEST_EXAMPLE_LANGGRAPH_PYTHON    langgraph 1.2.22 + langchain-mcp-adapters 0.3.2
CI's `framework-examples` job sets both, and CRASHTEST_REQUIRE_EXAMPLES so that a
missing one fails instead of skipping. Each example uses a scripted model, so
no API key is involved; REFUND_KEYING=naive must FAIL (a duplicate) and the
default, stable_key, must PASS.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from crashtest import refund_run as rr

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "frameworks"
FRAMEWORKS = {
    "pydantic_ai": ("pydantic_ai_agent.py", "CRASHTEST_EXAMPLE_PYDANTIC_AI_PYTHON"),
    "langgraph": ("langgraph_agent.py", "CRASHTEST_EXAMPLE_LANGGRAPH_PYTHON"),
}


@pytest.mark.parametrize("framework", sorted(FRAMEWORKS))
@pytest.mark.parametrize(("keying", "result", "verdict"), [
    ("naive", "FAIL", "duplicate"), ("stable_key", "PASS", "correct"),
])  # fmt: skip
def test_framework_example(stack, monkeypatch, framework, keying, result, verdict):
    script, env = FRAMEWORKS[framework]
    python = os.environ.get(env)
    if not python and os.environ.get("CRASHTEST_REQUIRE_EXAMPLES"):
        pytest.fail(f"{env} is not set")
    if not python:
        pytest.skip(f"set {env} to a Python with the {framework} example's requirements")
    monkeypatch.setenv("REFUND_KEYING", keying)
    run = rr.run_refunds(
        agent=f"{framework}-{keying}",
        agent_cmd=[python, str(EXAMPLES / script)],
        mcp_url=stack.mcp_url,
        sink_url=stack.sink_url,
        trials=2,
        control_token=stack.control_token,
        progress=False,
    )
    assert run.result == result, (run.summary, [t.agent_note for t in run.trials])
    assert [t.verdict for t in run.trials] == [verdict, verdict]
    assert all(t.fault_fired for t in run.trials)
