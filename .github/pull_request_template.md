## What this changes

<!-- One or two sentences. Link the issue it closes, e.g. "Closes #9". -->

## How it was tested

<!-- The commands you ran and what they printed. For a new example agent or client,
     paste the summary line of `crashtest run refunds --agent ...`. -->

## Checklist

- [ ] `ruff check src tests examples`, `ruff format --check src tests examples`, `mypy` and `pytest` pass locally (see docs/testing.md)
- [ ] No verdict, checker or contract change; or, if there is one, it is explained above and `tests/test_verdict_properties.py` was updated on purpose
- [ ] New behaviour has a test, and claims in the docs are things a test or a run shows
- [ ] No credentials, tokens, personal data or machine-specific paths in code, tests, docs or results
- [ ] `CHANGELOG.md` has a line for any user-visible change
