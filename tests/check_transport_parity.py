"""CI check on an installed package: the same agent gets the same verdict over stdio and HTTP.

Run with the clean-install environment's Python, from any folder:
    python tests/check_transport_parity.py DATA_DIR

It starts the local stack once, runs tests/data/transport_agent.py in both modes
(naive, stable_key) over both transports through `crashtest.refund_run`, and exits
1 unless naive FAILs with a duplicate and stable_key PASSes, identically over both.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from crashtest import refund_run as rr
from crashtest.experiment.local_stack import local_stack

AGENT = Path(__file__).resolve().parent / "data" / "transport_agent.py"
EXPECTED = {"naive": ("FAIL", "duplicate"), "stable_key": ("PASS", "correct")}


def main() -> int:
    data_dir = Path(sys.argv[1] if len(sys.argv) > 1 else ".crashtest-parity-pgdata").resolve()
    failures = []
    with local_stack(data_dir) as stack:
        os.environ["CRASHTEST_TEST_AGENT_SINK_URL"] = stack.sink_url
        for mode, (result, verdict) in EXPECTED.items():
            os.environ["CRASHTEST_TEST_AGENT_MODE"] = mode
            facts = {}
            for transport in ("http", "stdio"):
                os.environ["CRASHTEST_TEST_AGENT_TRANSPORT"] = transport
                run = rr.run_refunds(
                    agent=f"{mode}-{transport}",
                    agent_cmd=[sys.executable, str(AGENT)],
                    mcp_url=stack.mcp_url,
                    sink_url=stack.sink_url,
                    trials=2,
                    control_token=stack.control_token,
                    progress=False,
                )
                verdicts = [t.verdict for t in run.trials]
                print(f"{mode} over {transport}: {run.result} ({run.summary}); {verdicts}")
                if run.result != result or verdicts != [verdict] * 2:
                    failures.append(f"{mode} over {transport}: {run.result} {verdicts}")
                facts[transport] = (run.result, run.exit_code, run.observed()["verdicts"])
            if facts["http"] != facts["stdio"]:
                failures.append(f"{mode}: stdio {facts['stdio']} != http {facts['http']}")
    for f in failures:
        print(f"FAILED: {f}")
    print("transport parity: " + ("FAILED" if failures else "same verdicts over stdio and HTTP"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
