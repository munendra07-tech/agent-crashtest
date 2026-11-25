# Security

## This is a test rig

`agent-crashtest` is built to run on a developer machine or in CI. It is not a service to expose.

- The sink's control endpoints (`PUT /control/faults`, `POST /control/reset`) require a control token (`Authorization: Bearer <token>`) since 0.3.0. `crashtest serve` generates it and keeps it in `.crashtest-stack.json`; it is never sent over MCP and never given to an agent that `crashtest run` starts. An agent that can read that file can read the token, so keep the agent under test out of that folder. `GET /refunds`, which reads the ground truth, stays unauthenticated and read-only by design.
- Docker Compose binds every port to `127.0.0.1` only and refuses to start without `CRASHTEST_CONTROL_TOKEN`; the local stack (`--local`, `crashtest serve`) listens on `127.0.0.1`. Keep it that way.
- The HTTP MCP servers check the `Host` and `Origin` headers (DNS-rebinding protection), allowing only `127.0.0.1`, `localhost` and `[::1]`; Docker Compose adds its container names through `MCP_ALLOWED_HOSTS`. The test controller (`crashtest controller --http`) also requires the control token. Never configure the controller into an agent under test.
- The PostgreSQL password in `docker-compose.yml` (`crashtest`) is for a throwaway local database.

## How to report a problem

Report privately, through GitHub's **"Report a vulnerability"** button on this repository's **Security** tab (private vulnerability reporting). Please do not open a public issue or pull request for any of the cases below. Include what you found, where (file, line or endpoint), and how to reproduce it.

### A secret in the repository

If you find a credential anywhere in this repository or its history (an API key, a token, a password other than the throwaway one above, a private key), report it privately as above. Do not paste the value into the report: give the file and line or commit, and say what kind of credential it is. It will be revoked first and removed afterwards.

### A way around the sink

The sink is this project's source of truth: every result counts the refunds it committed. Report privately anything that would let a refund be made, or the count be changed, without the sink recording it correctly. For example:

- a request that creates a refund without a row in the request log, or the reverse;
- a way to make two refunds for one idempotency key, or to make a replay return a different refund;
- a way for a subject under test to reach the sink's database or control endpoints, so that it could change the ground truth it is judged by.

Any of these could make a published result wrong, so they matter even though the rig runs only locally.

### The rig reachable from outside

Anything that lets the rig be reached or controlled from beyond the local machine, or that lets a test run affect something outside the rig.

## Findings about other projects

If you find, with this rig, a duplicate-effect or lost-effect bug in another project, report it to that project's maintainers through their own security process first. Do not publish it here before they have had a chance to respond.
