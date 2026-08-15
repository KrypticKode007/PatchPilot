# Contributing to PatchPilot

Thank you for your interest in contributing to PatchPilot. This document outlines the process for submitting changes.

## Development Setup

```bash
git clone https://github.com/KrypticKode007/PatchPilot.git
cd PatchPilot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run with auto-reload
uvicorn patchpilot:app --reload --host 0.0.0.0 --port 8000
```

## Architecture Principles

- **Single-file architecture** — all application code lives in `patchpilot.py`. Do not split it into multiple modules.
- **Pydantic models** — add validation models for any new request or response types.
- **Sanitized errors** — full tracebacks go to server logs only. Clients see truncated, non-sensitive messages.
- **Environment variables** — all new configuration options must be env-configurable with sensible defaults.
- **No Rust-based dependencies** — PatchPilot targets Termux compatibility. Avoid packages that require Rust compilation (e.g., `ruff`, `watchfiles`).

## Before Submitting

### Syntax validation (required)

```bash
python -m py_compile patchpilot.py
python -c "import ast; ast.parse(open('patchpilot.py').read()); print('OK')"
```

### Functional tests (required)

```bash
pip install python-multipart  # if not already installed
python -c "
from fastapi.testclient import TestClient
import patchpilot

with TestClient(patchpilot.app) as client:
    assert client.get('/').status_code == 200
    assert client.get('/health').status_code == 200
    assert client.get('/jobs').status_code == 200
    assert client.get('/jobs/nonexistent').status_code == 404
    print('All functional tests passed')
"
```

### Checklist

- [ ] `py_compile` passes
- [ ] `ast.parse` passes
- [ ] No duplicate function or class definitions
- [ ] Module imports cleanly
- [ ] All endpoints return expected status codes
- [ ] New env vars documented in both the module docstring and `README.md`
- [ ] `CHANGELOG.md` updated with the change
- [ ] No tracebacks or internal paths in API error responses

## Pull Request Process

1. Fork the repository and create a feature branch (`git checkout -b feature/my-feature`)
2. Make your changes in `patchpilot.py`
3. Run the syntax validation and functional tests above
4. Update `README.md` and `CHANGELOG.md` if applicable
5. Open a pull request using the PR template

## Code Style

- Use `from __future__ import annotations` for forward references
- Type hints on all function signatures
- Descriptive docstrings on all public functions and classes
- Section headers with `# ===` dividers for major code regions
- Logging via the `patchpilot` logger, not `print()`

## Reporting Issues

Use the provided bug report or feature request templates on the [Issues](https://github.com/KrypticKode007/PatchPilot/issues) page.

## License

By contributing, you agree that your contributions will be licensed under the GNU General Public License v3.0, consistent with the Matchering 2.0 dependency.

---

PatchPilot — Created by KrypticKode007 with Maestro AI
