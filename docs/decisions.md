# Engineering decisions

Decisions that shape the project, with the reason and what else was considered, newest last. A change to one of these starts as an issue.

| Date | Decision | Why | Alternatives considered |
|---|---|---|---|
| Nov 2026 | Verdicts come only from the sink's committed rows, through one checker (`crashtest.checker`) | What an agent reports is the thing under test and cannot be trusted; one function keeps every runner, the CLI and the controller consistent | Judging from the client's view |
| Nov 2026 | Scripted models in the harness and examples; real models only in a dedicated study (experiment 006) | Deterministic, free and reproducible, with no API keys in CI | Real models everywhere (cost, non-determinism) |
| Nov 2026 | A stable key enforced by the sink is the positive control, not a reference "safe ledger" product | Idempotency libraries already exist; a neutral measurement is what is missing | Building and promoting a ledger |
| Nov 2026 | MIT licence | Permissive and simple, used from the first release | Apache-2.0 |
| Nov 2026 | The GitHub Action lives in this repository (`action.yml`) | One version and one release for the CLI and the Action | A separate action repository |
| Nov 2026 | `crashtest run` takes an agent's `done` as confirmed | The harness cannot see inside an agent; a test shows that the `unconfirmed_success` verdict cannot occur on this path | Requiring every agent to report what it received |
| Nov 2026 | Windows `initdb` failures on non-ASCII paths are documented, not worked around | They come from the bundled PostgreSQL tools; ASCII paths work, and CI's Windows jobs pass | Patching around the bundled binaries |
| Nov 2026 | MCP-first, with separate roles: the agent-facing server has only simulated tools; a token-protected test controller is for the developer; the sink is the only judge | The agent must not be able to arm faults, reset data or influence its own verdict | Control tools on the agent-facing server |
| Nov 2026 | New scenarios come from measured experiments (the cut before the request, from experiment 008), and each needs a defined fault, independent ground truth, reproducibility, documented limits and regression tests | Keeps every verdict defensible | Adding scenarios for coverage's sake |
| Nov 2026 | Reports carry a run ID, the scenario's name and version, a report schema version, and the versions and OS that ran the test; never host names, user names, paths or environment variables | Reports can be reproduced without collecting personal data | Full environment dumps |
