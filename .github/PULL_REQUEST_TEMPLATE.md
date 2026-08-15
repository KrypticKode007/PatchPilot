## Description

Brief description of the changes in this PR.

## Type of Change

- [ ] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature (non-breaking change that adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to not work as expected)
- [ ] Documentation update
- [ ] Refactor / code cleanup

## Changes Made

-
-
-

## Testing

- [ ] `python -m py_compile patchpilot.py` passes
- [ ] `ast.parse` passes
- [ ] Module imports cleanly
- [ ] Functional endpoint tests pass (see CONTRIBUTING.md)
- [ ] No tracebacks or internal paths in API error responses

## Checklist

- [ ] Code follows the single-file architecture (all changes in `patchpilot.py`)
- [ ] New env vars documented in module docstring and README.md
- [ ] CHANGELOG.md updated
- [ ] Pydantic models added for any new request/response types
- [ ] Logging uses the `patchpilot` logger, not `print()`
