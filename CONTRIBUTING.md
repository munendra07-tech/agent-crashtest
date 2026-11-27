# Contributing

Thanks for your interest. The project is early, so the most useful contributions are bug reports, reproductions, reports of agents that failed (or passed) `crashtest run refunds`, and new fault scenarios. Issue templates are provided for a tester report (any result, pass or fail, including a self-test only), a bug, a question, and "my agent failed". Everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md).

## Set up

[README.md](README.md) covers setup on Windows, on Linux or macOS, and with Docker. In short, on Linux or macOS:

```bash
python3.12 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
```

## Before opening a pull request

All four must pass; CI runs the same checks.

```bash
ruff check src tests examples
```

```bash
ruff format --check src tests examples
```

```bash
mypy
```

```bash
pytest
```

[docs/testing.md](docs/testing.md) explains the suite, faster variants and what CI runs. The pull-request template has a short checklist.

## Good first issues

Small, self-contained tasks, each a good first pull request. Open an issue first to say you are taking one.

- **An example agent for another MCP client or framework.** A script in `examples/` with the same arguments and JSON report as `examples/stable_key_agent.py`, built on another client library or agent framework, with the result of `crashtest run refunds --agent` on it in the pull request.
- **Try a client guide that is not yet verified.** Follow one of the Claude Desktop, Claude Code, VS Code or Cursor guides in [docs/clients/](docs/clients/README.md) and report what happened. A pull request marking it verified, with the client version and date, is welcome.
- **An example agent for another framework.** Like `examples/frameworks/`, with a scripted model so it needs no API key, and a CI check (issue #9).

## Rules for results

- A result must be reproducible from a results file in `results/` and a command in its experiment write-up.
- Write-ups state what was run, the versions, and what the result does **not** show.
- Do not discard runs that look wrong. Keep them and explain them, as experiment 001 does.
- Before publishing a result about another project, share the reproduction with its maintainers first.

## Scope

Faults belong in the sink or the injector, not in the subject under test. A subject should run unmodified, so the result reflects the subject and not the harness.
