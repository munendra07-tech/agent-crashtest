# The research behind agent-crashtest

`agent-crashtest` started as a crash-consistency testbench, and the experiments below are where the tool's scenarios and verdicts come from. This page was the second half of the README until 28 November 2026; its text is unchanged apart from this introduction. For the tool itself, start at the [README](../README.md).

**Status: early.** Eight experiments run end to end: two on the reference MCP client, one on three MCP client libraries, one that kills and resumes a LangGraph agent, one that kills and recovers a pydantic-ai agent on DBOS, one on what real models do after a tool reports an unknown outcome, one on whether the MCP client resumes a dropped stream, and one on cutting the connection before the commit. Known gaps are listed in [docs/audits/implementation-gap-report.md](audits/implementation-gap-report.md).

## First result

A refund issued through an MCP tool, where the server commits the refund and the response is lost:

| Client strategy on retry | Trials | Refunded once | Refunded twice |
|---|---|---|---|
| Retry with no key | 20 | 0 | 20 |
| Retry with a stable idempotency key | 20 | 20 | 0 |
| Retry with a new key each attempt | 20 | 0 | 20 |

In every double-refund trial the client believed it had made one refund. Details, versions and limits: [docs/experiments/001-mcp-refund-lost-response.md](experiments/001-mcp-refund-lost-response.md).

This result is expected; it validates the harness. It was run locally without Docker, three times. The table is from one run; the other two gave the same counts in these cells, except for one unplanned triple refund in a different cell, explained in the write-up.

## Second result

The same test against four MCP server settings, with a second fault that cuts the connection instead of delaying the response: the `mcp` Python client resent nothing by itself in 720 trials, and the outcome depended only on the key strategy. A later check (experiment 007) showed that the client's default protocol, 2026-07-28, made the four server settings one transport on the wire, so this measured one transport four times. Details and limits: [docs/experiments/002-mcp-client-matrix.md](experiments/002-mcp-client-matrix.md).

## Try it: a local MCP refund server, an unsafe client and a safe one

The project includes an MCP server with one fake refund tool (`issue_refund`: it writes a row to a local test database and moves no real money) and a sink that records every refund, so you can see whether a client refunded once, twice or not at all. It all runs on your machine. To point your own MCP client at it and read the sink's verdict, see [docs/connect-your-client.md](connect-your-client.md).

The fault: the sink commits the refund, then holds its response past the client's timeout, so the client never hears back. Each command runs three trials on a local stack (no Docker needed) and prints a table:

```powershell
python -m crashtest.experiment.run --local --faults lost_response --arms no_dedup --trials 3 --out-dir demo-results
```

```powershell
python -m crashtest.experiment.run --local --faults lost_response --arms stable_key --trials 3 --out-dir demo-results
```

The first client retries with no idempotency key: expect `duplicate` 3, `effects_committed` 6, and `report_mismatch` 3 (each time the client said one refund was made, and the sink holds two). The second retries with a key that stays the same across attempts: expect `correct` 3, `effects_committed` 3, `report_mismatch` 0. Checked on Windows on 20 November 2026, about 15 seconds each, and on Ubuntu (GitHub runner, 20 November UTC) with both arms in one run (`results/mcp_refund_linux/20261120T205010Z.json`, through `.github/workflows/experiment.yml`): the same counts. The JSON with every trial goes to `demo-results/`.

To point your own MCP client at the same tool and fault, see [docs/connect-your-client.md](connect-your-client.md).

## Further results

- **MCP client libraries (003).** crewAI 1.15.41's MCP client sends a timed-out tool call again by default, and refunded twice while reporting success (20 of 20). agent-framework and the reference client did not. This behaviour is already public as crewAI issue #8932. Only CrewAI's two transports really differed; the other two clients used one protocol on both. [Write-up](experiments/003-mcp-client-libraries.md).
- **LangGraph, killed and resumed (004).** A tool interrupted mid-call runs again on resume. Under LangGraph's default `async` mode, a tool that had already returned also ran again on Linux, because its result was not yet checkpointed (on Windows it was). A key taken from the model's tool-call ID held except under `exit` durability, where nothing had been saved and the model issued a new ID; a key derived from the order held everywhere. [Write-up](experiments/004-langgraph-crash-resume.md).
- **pydantic-ai on DBOS, killed and recovered (005).** A plain function tool ran again even after it had returned; a `@DBOS.step` or child-workflow tool ran again only when killed mid-call. Every key tested held. With step retries turned on, a late response alone, with no crash, refunded twice. All documented behaviour, not defects. [Write-up](experiments/005-pydantic-ai-dbos.md).
- **Real models after an unknown outcome (006).** Opus 5.5, Sonnet 5.5 and Haiku 5.5, 930 trials. After "internal server error" with no way to check, Sonnet 5.5 sent the refund again in 34 of 50 trials, refunding twice, and in 32 of those told the user only one refund was made; in every other faulted condition no model re-sent it (0 of 300 for Opus, 0 of 250 for Sonnet, 0 of 300 for Haiku). Given a tool to look up the order's refunds, every model looked them up, before the refund and again after the error, and none re-sent (150 of 150 per model). Every re-sent call repeated the arguments exactly, so an argument-hash key would have caught all of them. [Write-up](experiments/006-model-retry.md); [validation against the sink](experiments/006-validation.md).
- **Resuming a dropped MCP stream (007).** The reference `mcp` client 2.3.0 resumed a dropped call from a server's event store, and refunded once even with a naive retry, only with the 2025-11-25 protocol (`mode="legacy"`), a stateful SSE server with an event store, and a drop after the stream's priming event (20 of 20 per strategy). With its default settings it negotiates 2026-07-28, which has no resumable streams, and a naive retry refunded twice in every trial. [Write-up](experiments/007-mcp-resume.md).
- **Cutting the connection before the commit (008).** With the reference `mcp` client 2.3.0 and a tool that reports progress and pauses before its effect, a cut before the request reached the server, or mid-stream while the tool was still working, left no refund from the first attempt, so even a naive retry refunded once (120 of 120 per cut point, on Windows and on Linux). A cut before the request raised the same exception as a cut after the commit, where the naive retry refunded twice: the caller cannot tell the two apart from the error. A cut during the effect itself was not tested, and neither was a silent drop: the proxy aborted both sides at once, so the server always learned of the cut. [docs/experiments/008-mcp-cut-points.md](experiments/008-mcp-cut-points.md).
- **The two durable engines side by side:** [docs/comparison-langgraph-dbos.md](comparison-langgraph-dbos.md).

Across the first five, the one defence that held in every configuration was an idempotency key derived from the business operation and enforced by the database that performs the effect.

## Results table

Every published run, by experiment, machine and run condition. Regenerate with `python -m crashtest.results.table --write`; the cell-by-cell table is [docs/results.md](results.md). A test checks that both match the results files.

<!-- results-table:start (generated by python -m crashtest.results.table --write) -->
| Exp. | Machine | Run conditions | Files | Trials | Refunded once | Twice or more | None | Verdicts | Differs |
|---|---|---|---|---|---|---|---|---|---|
| [001](experiments/001-mcp-refund-lost-response.md) | Windows | client_timeout_s=0.5, fault_delay_s=2.0 | 2 | 240 | 159 | 81 | 0 | correct 159, duplicate 81 | 0 |
| [001](experiments/001-mcp-refund-lost-response.md) | Windows | client_timeout_s=2.0, fault_delay_s=5.0 | 1 | 120 | 80 | 40 | 0 | correct 80, duplicate 40 | 0 |
| [002](experiments/002-mcp-client-matrix.md) | Linux | client_timeout_s=2.0, fault_delay_s=1.0 | 1 | 60 | 60 | 0 | 0 | correct 60 | 0 |
| [002](experiments/002-mcp-client-matrix.md) | Linux | client_timeout_s=2.0, fault_delay_s=1.8 | 1 | 60 | 60 | 0 | 0 | correct 60 | 0 |
| [002](experiments/002-mcp-client-matrix.md) | Linux | client_timeout_s=2.0, fault_delay_s=2.2 | 1 | 60 | 20 | 40 | 0 | duplicate 40, correct 20 | 0 |
| [002](experiments/002-mcp-client-matrix.md) | Linux | client_timeout_s=2.0, fault_delay_s=4.0 | 1 | 60 | 20 | 40 | 0 | duplicate 40, correct 20 | 0 |
| [002](experiments/002-mcp-client-matrix.md) | Linux | client_timeout_s=2.0, fault_delay_s=5.0 | 1 | 6 | 3 | 3 | 0 | correct 3, duplicate 3 | 0 |
| [002](experiments/002-mcp-client-matrix.md) | Linux | client_timeout_s=2.0, fault_delay_s=8.0 | 1 | 60 | 20 | 40 | 0 | duplicate 40, correct 20 | 0 |
| [002](experiments/002-mcp-client-matrix.md) | Windows | client_timeout_s=0.5, fault_delay_s=2.0 | 1 | 120 | 80 | 40 | 0 | correct 80, duplicate 40 | 0 |
| [002](experiments/002-mcp-client-matrix.md) | Windows | client_timeout_s=2.0, fault_delay_s=5.0 | 2 | 1440 | 800 | 640 | 0 | correct 800, duplicate 640 | 0 |
| [003](experiments/003-mcp-client-libraries.md) | Linux | subject_timeout_s=2.0, fault_delay_s=1.0 | 1 | 30 | 30 | 0 | 0 | correct 30 | 0 |
| [003](experiments/003-mcp-client-libraries.md) | Linux | subject_timeout_s=2.0, fault_delay_s=1.8 | 1 | 30 | 30 | 0 | 0 | correct 30 | 0 |
| [003](experiments/003-mcp-client-libraries.md) | Linux | subject_timeout_s=2.0, fault_delay_s=2.2 | 1 | 30 | 20 | 10 | 0 | in_doubt_surfaced 20, duplicate 10 | 0 |
| [003](experiments/003-mcp-client-libraries.md) | Linux | subject_timeout_s=2.0, fault_delay_s=4.0 | 1 | 30 | 20 | 10 | 0 | in_doubt_surfaced 20, duplicate 10 | 0 |
| [003](experiments/003-mcp-client-libraries.md) | Linux | subject_timeout_s=2.0, fault_delay_s=8.0 | 2 | 210 | 180 | 30 | 0 | in_doubt_surfaced 120, correct 60, duplicate 30 | 0 |
| [003](experiments/003-mcp-client-libraries.md) | Windows | subject_timeout_s=0.0, fault_delay_s=40.0 | 1 | 3 | 0 | 3 | 0 | duplicate 3 | 0 |
| [003](experiments/003-mcp-client-libraries.md) | Windows | subject_timeout_s=2.0, fault_delay_s=8.0 | 2 | 198 | 176 | 22 | 0 | in_doubt_surfaced 110, correct 66, duplicate 22 | 0 |
| [004](experiments/004-langgraph-crash-resume.md) | Linux | - | 1 | 135 | 100 | 35 | 0 | correct 100, duplicate 35 | 0 |
| [004](experiments/004-langgraph-crash-resume.md) | Windows | - | 3 | 450 | 390 | 60 | 0 | correct 390, duplicate 60 | 0 |
| [005](experiments/005-pydantic-ai-dbos.md) | Linux | system_database=postgres | 1 | 360 | 320 | 40 | 0 | correct 320, duplicate 40 | 0 |
| [005](experiments/005-pydantic-ai-dbos.md) | Windows | system_database=postgres | 4 | 1140 | 1011 | 129 | 0 | correct 970, duplicate 129, stuck 41 | 0 |
| [005](experiments/005-pydantic-ai-dbos.md) | Windows | system_database=sqlite | 1 | 90 | 50 | 40 | 0 | correct 50, duplicate 40 | 0 |
| [006](experiments/006-model-retry.md) | Windows | run=main | 1 | 930 | 896 | 34 | 0 | correct 478, in_doubt_surfaced 406, duplicate 34, stuck 9, unconfirmed_success 3 (recorded) | - |
| [006](experiments/006-model-retry.md) | Windows | run=pilot | 5 | 117 | 112 | 2 | 3 | correct 45, in_doubt_surfaced 39, stuck 21, unconfirmed_success 7, no verdict recorded 3, duplicate 2 (recorded) | - |
| [007](experiments/007-mcp-resume.md) | Linux | client_timeout_s=5.0 | 1 | 720 | 440 | 280 | 0 | correct 440, duplicate 280 | 0 |
| [007](experiments/007-mcp-resume.md) | Windows | client_timeout_s=5.0 | 1 | 720 | 440 | 280 | 0 | correct 440, duplicate 280 | 0 |
| [008](experiments/008-mcp-cut-points.md) | Linux | client_timeout_s=10.0 | 1 | 480 | 400 | 80 | 0 | correct 400, duplicate 80 | 0 |
| [008](experiments/008-mcp-cut-points.md) | Windows | client_timeout_s=10.0 | 1 | 480 | 400 | 80 | 0 | correct 400, duplicate 80 | 0 |

Verdicts are re-derived with the current checker from the recorded trials, taking every refund's amount as correct (only the sink database can check amounts); "Differs" counts trials whose recorded verdict is different. 006 shows its recorded verdicts.

Not judged (written before the checker, no verdicts recorded): `results/single_call/20261116T165233Z.json` (180 trials); `results/single_call/20261116T171353Z.json` (3 trials).

Per cell, with notes on 002 and 006: [results.md](results.md).
<!-- results-table:end -->

## How the experiments work

```
experiment runner ──> MCP client ──> refund MCP server ──> sink (FastAPI) ──> PostgreSQL
        │                                                      ▲
        └────────── arm fault / read ground truth ─────────────┘
```

- The **sink** plays the outside world. Each row in its `refunds` table is an effect that happened.
- The **fault** makes the sink commit a refund and then withhold the response past the client's timeout.
- The **verdict** for a trial is the number of refund rows. What the client believes is recorded but not trusted.

More: [docs/architecture.md](architecture.md), [docs/contract.md](contract.md).

## Related work

This project builds on, and must be read alongside, "Resume Means Resume" (arXiv 2608.03836), UndoBench (arXiv 2610.05622) and ACRFence (arXiv 2603.20625). What each covers and what this project adds: [docs/related-work.md](related-work.md).

## Provenance and review

See [ROADMAP.md](../ROADMAP.md). Results files record the commit they were run from; see [PROVENANCE.md](../PROVENANCE.md).

An adversarial review of the experiments' methods and conclusions is in [docs/audits/scientific-audit.md](audits/scientific-audit.md).
