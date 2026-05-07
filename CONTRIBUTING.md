# Contributing to hermes-icm-memory

Thanks for considering a contribution. This project is a thin Hermes Agent memory provider plugin that shells out to the [`icm`](https://github.com/rtk-ai/icm) CLI; the public surface is intentionally small and frozen post-v1, so most contributions land in tests, docs, or one of the internal modules (`cli_runner`, `config`, `mapping`, `provider`, `hooks`, `tools`).

## Dev install

The project targets Python 3.11+ and uses an editable install for development:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Optional but recommended: have `icm` on your PATH so the integration tests under `tests/integration/` can run end-to-end. Unit tests mock `subprocess.run` and do not require `icm`.

## Dev loop

Run all three gates locally before opening a PR — CI runs the same three:

```bash
ruff check .
mypy --strict hermes_icm_memory
pytest
```

`pytest` enforces a branch-coverage gate of **85 %** via `--cov-fail-under=85` (configured in `pyproject.toml`). New code must keep the suite at or above that floor.

## TDD policy

This project is **TDD-required**. Every code change starts with a failing test:

1. Write the failing test that captures the new behavior.
2. Run `pytest <path>::<test>` and confirm it fails for the right reason.
3. Implement the smallest change that turns it green.
4. Refactor, then re-run `ruff check .`, `mypy --strict hermes_icm_memory`, and the full `pytest` suite.

Story files under `_bmad-output/implementation-artifacts/` document the test plan for each unit of work; mirror that style when you add tests.

## Quality bar

- `ruff check .` is clean (lint rules listed in `pyproject.toml` `[tool.ruff.lint]`).
- `mypy --strict` is clean. Every public function and class has full type hints.
- `pytest` is green and branch coverage stays at or above 85 %.
- `cli_runner.py` is the only module that imports `subprocess`. Any new code that needs to invoke `icm` must go through it.
- No network I/O, ever. No hard-coded `~/.hermes` paths — everything derives from `kwargs['hermes_home']`.

## Commit messages

- Short imperative subject (e.g. `feat(S07): add IcmMemoryProvider class`, `fix(cli_runner): handle empty stdout`).
- Body wrapped at ~72 columns when one is needed; explain *why*, not *what*.
- **No `Co-Authored-By` lines.** This is a project-wide convention.
- Prefer one logical change per commit; keep diffs reviewable.

## Pull requests

- Link to the relevant story file under `_bmad-output/implementation-artifacts/` if your change implements one.
- Include a one-paragraph summary and a checklist of which gates you ran locally.
- CI must be green before merge. Reviewers will look for: TDD discipline (tests in the diff), coverage holding, lint + mypy clean, and that no new code reaches around `cli_runner` to import `subprocess`.

## License

By contributing, you agree that your contributions are licensed under the project's [BSD 3-Clause](./LICENSE) license.
