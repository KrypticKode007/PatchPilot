# Changelog

All notable changes to PatchPilot will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] — 2026-08-15

### Added
- Complete single-file application: `patchpilot.py` (FastAPI + job queue + Matchering)
- Two processing modes: `direct` (in-process Python API) and `subprocess` (external CLI via `asyncio.create_subprocess_exec`)
- Streaming chunked file uploads (1 MB chunks, constant memory)
- Pydantic v2 validation models for all request/response types
- Request-ID middleware with `X-Request-ID` response header
- Global unhandled-exception handler
- CORS middleware with env-configurable origins (defaults to localhost)
- Startup checks: directories, matchering, soundfile/libsndfile, ffmpeg, matchering-cli
- Sanitized client-facing error messages (full tracebacks in server logs only)
- Path-traversal protection on all file paths
- Subprocess timeout handling with process kill/reap on timeout
- Graceful shutdown (worker cancellation, executor shutdown)
- Environment-variable configuration for all tunable parameters
- `requirements.txt` with pinned version constraints
- Comprehensive `README.md` with architecture diagram, API reference, and usage examples
- GitHub project structure: `.gitignore`, `LICENSE`, `CONTRIBUTING.md`, `CHANGELOG.md`
- CI workflow: syntax validation and functional endpoint tests
- Issue templates: bug report and feature request
- Pull request template

### Security
- Download filenames sanitized to basename only
- CORS defaults to localhost origins
- Error responses do not expose internal paths or tracebacks
- Uploaded files saved with UUID-based names

## [1.0.0] — 2026-08-15

### Added
- Initial `integrated_matchering_api.py` combining FastAPI backend, ThreadPoolExecutor job queue, and Matchering 2.0 integration
- Multipart file upload endpoints with extension and size validation
- Direct and subprocess processing modes
- In-memory job store with async locking
- Startup checks and health endpoint

[2.0.0]: https://github.com/KrypticKode007/PatchPilot/releases/tag/v2.0.0
[1.0.0]: https://github.com/KrypticKode007/PatchPilot/releases/tag/v1.0.0
