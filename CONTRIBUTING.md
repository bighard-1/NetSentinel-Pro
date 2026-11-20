# Contributing

Thanks for helping improve NetSentinel Pro. Check existing issues and the [security policy](SECURITY.md) before reporting a problem.

## Local setup

Use Python 3.12 and create a virtual environment in the repository. Install runtime and development requirements:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
```

Set `ADMIN_USER`, `ADMIN_PASS`, and `APP_SECRET` in your local environment. Keep secrets and database files out of commits. Run one worker and use a separate test database.

## Before a pull request

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/self_check.py
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy version.py services/state_machine.py services/availability.py services/retention.py auth.py alert.py
.venv/bin/python -m pytest -q --cov --cov-report=json:coverage.json
.venv/bin/python scripts/check_core_coverage.py coverage.json
```

Describe the change, how you tested it, and any database migration or rollback steps. Preserve existing API paths and backup compatibility unless the change is explicitly versioned. For schema changes, add an Alembic migration and test upgrade from an older database.

The browser test in `tests/e2e/test_dashboard.py` needs Playwright and a Chromium-compatible browser. Place its temporary files and browser cache on a disk with enough free space.
