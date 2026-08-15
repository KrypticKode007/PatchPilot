# PatchPilot

### Audio Mastering API

A single-file, production-ready Python application that combines a FastAPI backend, a background job queue, and [Matchering 2.0](https://github.com/sergree/matchering) audio matching/mastering integration into one cohesive service.

Upload a target track and a reference track, submit a mastering job, poll for completion, and download the mastered result — all through a clean REST API.

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running](#running)
- [API Reference](#api-reference)
- [Usage Examples](#usage-examples)
- [Processing Modes](#processing-modes)
- [Production Notes](#production-notes)
- [Project Structure](#project-structure)
- [Contributing](#contributing)
- [License](#license)

---

## Features

- **REST API** for uploading target + reference audio, submitting mastering jobs, polling job status, listing jobs, cancelling queued jobs, and downloading mastered results.
- **Background job queue** using `asyncio.Queue` with a dedicated async worker loop.
- **Two processing modes:**
  - `direct` — in-process Matchering Python API (blocking call dispatched to a `ThreadPoolExecutor` so the event loop stays free).
  - `subprocess` — external `mg_cli.py` CLI invoked via `asyncio.create_subprocess_exec` with `asyncio.wait_for` timeout and proper process kill/reap on timeout.
- **Pydantic v2 validation models** for every request and response.
- **Streaming multipart uploads** — files written in 1 MB chunks, never fully loaded into memory. Extension validation, size enforcement, and path-traversal protection.
- **Subprocess timeout handling** — hard cap per job, process killed and reaped on timeout, truncated stdout/stderr capture.
- **Robust structured error logging:**
  - Request-ID middleware (unique ID per request in logs and `X-Request-ID` response header).
  - Per-job exception capture with full tracebacks in server logs.
  - Sanitized client-facing error messages (no internal paths or tracebacks exposed to API consumers).
  - Global unhandled-exception handler.
- **Startup checks** — verifies directories, libsndfile (via soundfile), FFmpeg, Matchering import, and matchering-cli availability. Results exposed through `GET /health`.
- **Graceful shutdown** — worker task cancellation and executor shutdown on `SIGTERM`/`SIGINT`.
- **Environment-variable configuration** — no config files needed; every tunable parameter is env-configurable.
- **CORS support** — defaults to localhost origins; configurable for production deployments.

---

## Architecture

```
                     ┌──────────────────────────────────────────┐
                     │              PatchPilot                  │
                     │                                          │
   HTTP Request ───► │  FastAPI + Middleware (Request ID, CORS) │
                     │         │                                │
                     │    ┌────▼─────┐                          │
                     │    │ Endpoint │  POST /jobs              │
                     │    │ layer    │  GET  /jobs/{id}         │
                     │    │          │  GET  /jobs/{id}/download│
                     │    │          │  DELETE /jobs/{id}        │
                     │    │          │  GET  /jobs               │
                     │    │          │  GET  /health            │
                     │    └────┬─────┘                          │
                     │         │                                │
                     │    ┌────▼─────┐                          │
                     │    │ JobStore  │  (in-memory, async lock) │
                     │    └────┬─────┘                          │
                     │         │ enqueue job_id                 │
                     │    ┌────▼─────┐                          │
                     │    │ asyncio  │  (bounded queue)         │
                     │    │ Queue    │                          │
                     │    └────┬─────┘                          │
                     │         │ dequeue                        │
                     │    ┌────▼─────┐                          │
                     │    │ Worker   │  (async loop)            │
                     │    │ Loop     │                          │
                     │    └──┬───┬───┘                          │
                     │       │   │                              │
                     │  direct   subprocess                     │
                     │   mode    mode                            │
                     │    │       │                              │
                     │  ┌─▼───┐  ┌▼──────────────────┐          │
                     │  │ TPE │  │ asyncio            │          │
                     │  │     │  │ .create_subprocess│          │
                     │  │ mg. │  │ _exec + wait_for  │          │
                     │  │proc │  │ + kill/reap       │          │
                     │  └─────┘  └───────────────────┘          │
                     └──────────────────────────────────────────┘
```

### Components

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Web framework | FastAPI | REST endpoints, automatic OpenAPI docs |
| Validation | Pydantic v2 | Request/response models with field constraints |
| Job queue | `asyncio.Queue` | Bounded in-memory queue for mastering jobs |
| Job store | `JobStore` (in-memory) | Async-locked dict tracking all job records |
| Direct processing | `ThreadPoolExecutor` | Runs blocking `mg.process()` off the event loop |
| Subprocess processing | `asyncio.create_subprocess_exec` | Non-blocking CLI invocation with timeout |
| Audio mastering | Matchering 2.0 | Open-source spectral matching and mastering |
| Audio I/O | soundfile / libsndfile | WAV/FLAC/OGG read/write |

---

## Requirements

### Python

- Python 3.10 or higher (uses `match` statements, `X | Y` union syntax, `from __future__ import annotations`)

### Python packages

See [requirements.txt](requirements.txt):

```
fastapi>=0.115.0,<1.0.0
uvicorn[standard]>=0.30.0,<1.0.0
pydantic>=2.9.0,<3.0.0
python-multipart>=0.0.12
matchering>=2.0.6
soundfile>=0.12.0
```

### System dependencies

| Dependency | Debian/Ubuntu | macOS (Homebrew) | Termux (Android) |
|-----------|---------------|-------------------|-------------------|
| libsndfile | `sudo apt install libsndfile1` | `brew install libsndfile` | `pkg install libsndfile` |
| FFmpeg | `sudo apt install ffmpeg` | `brew install ffmpeg` | `pkg install ffmpeg` |

FFmpeg is optional (needed only for MP3 input). libsndfile is required for all audio I/O.

### Optional: matchering-cli (subprocess mode)

If you want to use `mode=subprocess` instead of `mode=direct`, install the [matchering-cli](https://github.com/sergree/matchering-cli) script separately:

```bash
git clone https://github.com/sergree/matchering-cli.git
```

PatchPilot will auto-detect `mg_cli.py` on `PATH` or near the matchering package.

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/KrypticKode007/PatchPilot.git
cd PatchPilot

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
# .venv\Scripts\activate         # Windows

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Install system dependencies (if not already present)
sudo apt install libsndfile1 ffmpeg        # Debian/Ubuntu
# brew install libsndfile ffmpeg            # macOS
# pkg install libsndfile ffmpeg             # Termux
```

---

## Configuration

All configuration is via environment variables. No config files required.

| Variable | Default | Description |
|----------|---------|-------------|
| `PATCHPILOT_HOST` | `0.0.0.0` | Listen host |
| `PATCHPILOT_PORT` | `8000` | Listen port |
| `PATCHPILOT_UPLOAD_DIR` | `./uploads` | Directory for uploaded audio files |
| `PATCHPILOT_OUTPUT_DIR` | `./outputs` | Directory for mastered output files |
| `PATCHPILOT_TEMP_DIR` | `./tmp` | Temporary working directory for Matchering |
| `PATCHPILOT_MAX_UPLOAD_MB` | `200` | Maximum upload size per file (in MB) |
| `PATCHPILOT_MAX_WORKERS` | `1` | Worker thread count (see warning below) |
| `PATCHPILOT_QUEUE_MAXSIZE` | `50` | Maximum jobs in the queue |
| `PATCHPILOT_TIMEOUT_SECONDS` | `1800` | Processing timeout per job (30 min default) |
| `PATCHPILOT_MATCHERING_CLI` | `mg_cli.py` | matchering-cli script name/path |
| `PATCHPILOT_CORS_ORIGINS` | `localhost` | CORS origins (comma-separated or `*`) |

> **Warning:** `MAX_WORKERS` defaults to `1` because Matchering's Python API uses module-level state (logging handlers, temp files) that is not guaranteed thread-safe. If you set `MAX_WORKERS > 1`, use `mode=subprocess` or accept the risk of concurrent direct-mode calls.

### CORS configuration examples

```bash
# Default (localhost only)
export PATCHPILOT_CORS_ORIGINS=""

# Specific origins
export PATCHPILOT_CORS_ORIGINS="https://app.example.com,https://staging.example.com"

# Allow all origins (credentials disabled automatically)
export PATCHPILOT_CORS_ORIGINS="*"
```

---

## Running

### Direct

```bash
python patchpilot.py
```

### With Uvicorn (development with auto-reload)

```bash
uvicorn patchpilot:app --reload --host 0.0.0.0 --port 8000
```

### With environment overrides

```bash
PATCHPILOT_PORT=9000 \
PATCHPILOT_MAX_UPLOAD_MB=500 \
PATCHPILOT_TIMEOUT_SECONDS=3600 \
python patchpilot.py
```

Once running, open the interactive API docs:

- **Swagger UI:** http://localhost:8000/docs
- **ReDoc:** http://localhost:8000/redoc
- **Health check:** http://localhost:8000/health

---

## API Reference

### `POST /jobs` — Create a mastering job

Upload target and reference audio files, queue a mastering job.

**Content-Type:** `multipart/form-data`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `target_file` | file (required) | — | The track to master |
| `reference_file` | file (required) | — | The reference track to match |
| `bit_depth` | int | `16` | Output bit depth (16, 24, or 32) |
| `normalize` | bool | `true` | Apply normalization |
| `use_limiter` | bool | `true` | Apply final-stage limiter |
| `mode` | string | `direct` | Processing mode (`direct` or `subprocess`) |
| `sample_rate` | int | `44100` | Internal sample rate (8000–192000) |
| `max_length` | float | `900.0` | Max audio length in seconds (max 7200) |

**Returns:** `202 Accepted` with `{ job_id, status, message }`

**Allowed extensions:** `.wav`, `.flac`, `.mp3`, `.aiff`, `.aif`, `.ogg`, `.m4a`

---

### `GET /jobs/{job_id}` — Get job status

Returns current status, progress, logs, error, and download URL.

**Returns:** `200 OK` with job details, or `404` if not found.

**Status values:** `queued`, `running`, `succeeded`, `failed`, `cancelled`

---

### `GET /jobs/{job_id}/download` — Download mastered file

Downloads the mastered WAV file for a completed job.

**Returns:** `200 OK` with `audio/wav` file response, `404` if not found, `409` if not completed.

---

### `DELETE /jobs/{job_id}` — Cancel a queued job

Only queued jobs can be cancelled. Running jobs must complete or fail.

**Returns:** `200 OK` with cancellation confirmation, `404` if not found, `409` if running/completed.

---

### `GET /jobs` — List recent jobs

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | `50` | Maximum number of jobs to return |

**Returns:** `200 OK` with array of job status objects (most recent first).

---

### `GET /health` — Service health

Returns service status, version, startup check results, and queue metrics.

**Returns:** `200 OK` with `{ status, version, checks, active_jobs, queued_jobs }`

---

## Usage Examples

### Submit a mastering job with curl

```bash
curl -X POST http://localhost:8000/jobs \
  -F "target_file=@my_song.wav" \
  -F "reference_file=@reference_track.wav" \
  -F "bit_depth=24" \
  -F "normalize=true" \
  -F "use_limiter=true" \
  -F "mode=direct"
```

Response:
```json
{
  "job_id": "a1b2c3d4e5f6...",
  "status": "queued",
  "message": "Job queued successfully. Poll GET /jobs/{job_id} for status."
}
```

### Poll job status

```bash
curl http://localhost:8000/jobs/a1b2c3d4e5f6...
```

Response:
```json
{
  "job_id": "a1b2c3d4e5f6...",
  "status": "succeeded",
  "progress": 1.0,
  "created_at": 1723755422.0,
  "started_at": 1723755422.1,
  "finished_at": 1723755480.5,
  "error": null,
  "logs": ["Job started.", "Processing completed successfully."],
  "download_url": "/jobs/a1b2c3d4e5f6.../download",
  "options": {
    "bit_depth": 24,
    "normalize": true,
    "use_limiter": true,
    "mode": "direct",
    "sample_rate": 44100,
    "max_length": 900.0
  }
}
```

### Download the mastered file

```bash
curl -OJ http://localhost:8000/jobs/a1b2c3d4e5f6.../download
```

### Using Python (requests)

```python
import requests
import time

# Submit job
with open("my_song.wav", "rb") as target, open("reference.wav", "rb") as ref:
    resp = requests.post(
        "http://localhost:8000/jobs",
        files={
            "target_file": target,
            "reference_file": ref,
        },
        data={"bit_depth": "16", "mode": "direct"},
    )
job_id = resp.json()["job_id"]

# Poll until complete
while True:
    status = requests.get(f"http://localhost:8000/jobs/{job_id}").json()
    if status["status"] in ("succeeded", "failed"):
        break
    time.sleep(2)

# Download result
if status["status"] == "succeeded":
    audio = requests.get(f"http://localhost:8000/jobs/{job_id}/download")
    with open("mastered.wav", "wb") as f:
        f.write(audio.content)
    print("Downloaded mastered.wav")
else:
    print(f"Job failed: {status['error']}")
```

### Check service health

```bash
curl http://localhost:8000/health | python -m json.tool
```

---

## Processing Modes

### Direct mode (`mode=direct`)

Uses the Matchering Python API (`mg.process()`) in-process. The blocking call is dispatched to a `ThreadPoolExecutor` so the FastAPI event loop stays responsive.

- No external CLI needed
- Matchering log messages are captured via custom handlers and appended to the job record
- Not thread-safe — `MAX_WORKERS` should remain `1`
- Uses `mg.Config` for sample rate, max length, and temp folder
- Uses `mg.Result` for output path, bit depth, limiter, and normalization

### Subprocess mode (`mode=subprocess`)

Invokes the external `mg_cli.py` CLI via `asyncio.create_subprocess_exec`. No thread is consumed — the subprocess runs asynchronously with the event loop.

- Requires [matchering-cli](https://github.com/sergree/matchering-cli) installed
- Non-blocking (no thread pool used)
- Timeout enforced via `asyncio.wait_for` — process is killed and reaped on timeout
- stdout/stderr captured, truncated to 200 lines, logged and appended to job record
- Safe for `MAX_WORKERS > 1`

CLI invocation:
```
python3 mg_cli.py <target> <reference> <output> -b{16|24|32} [--no_limiter] [--dont_normalize]
```

---

## Production Notes

### Single-process limitation

The in-memory job store (`JobStore`) and queue (`asyncio.Queue`) are suitable for a **single-process** Uvicorn deployment (`--workers 1`). For multi-worker or distributed production:

- Replace `JobStore` with Redis or a database
- Replace `asyncio.Queue` with Redis/Celery/RQ
- Use a shared filesystem (S3, NFS) for uploads/outputs

### Security

- Uploaded files are saved with UUID-based names; original filenames are preserved only as metadata
- Path traversal protection via `Path.relative_to()` validation on all file paths
- Error responses are sanitized — full tracebacks stay in server logs, clients see truncated messages
- Download filenames are sanitized to basename only
- CORS defaults to localhost; explicitly configure for production origins

### Performance

- Upload streaming: 1 MB chunks, constant memory regardless of file size
- Subprocess output capture: truncated to 200 lines per stream to prevent memory bloat
- Job logs in API responses: capped at last 100 lines
- Queue has a bounded maxsize (default 50) — rejects with 503 when full

### Termux (Android) notes

PatchPilot was developed with Termux compatibility in mind:

- No Rust-based dependencies required (no `ruff`, no `watchfiles`)
- `uvicorn[standard]` can be replaced with plain `uvicorn` if `watchfiles` fails to compile
- Use `pkg install libsndfile ffmpeg` for system dependencies
- Set `PATCHPILOT_HOST=127.0.0.1` for local-only access on mobile

---

## Project Structure

```
PatchPilot/
├── patchpilot.py                    # Complete application (single file)
├── requirements.txt                 # Python dependencies
├── README.md                        # Project documentation
├── LICENSE                          # GPL v3 (Matchering dependency)
├── CHANGELOG.md                     # Version history
├── CONTRIBUTING.md                  # Contribution guidelines
├── .gitignore                       # Git ignore rules
├── .github/
│   ├── workflows/
│   │   └── ci.yml                   # CI: syntax + functional tests
│   ├── ISSUE_TEMPLATE/
│   │   ├── bug_report.md            # Bug report template
│   │   └── feature_request.md       # Feature request template
│   └── PULL_REQUEST_TEMPLATE.md      # PR template
├── uploads/                         # Created at runtime (uploaded audio)
├── outputs/                         # Created at runtime (mastered audio)
└── tmp/                             # Created at runtime (Matchering temp)
```

---

## Contributing

Created by **KrypticKode007** on GitHub, with assistance from an AI software engineering student (Maestro AI).

See [CONTRIBUTING.md](CONTRIBUTING.md) for full guidelines, and [CHANGELOG.md](CHANGELOG.md) for version history.

### Development setup

```bash
git clone https://github.com/KrypticKode007/PatchPilot.git
cd PatchPilot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run with auto-reload
uvicorn patchpilot:app --reload
```

### Running tests

```bash
# Syntax validation
python -m py_compile patchpilot.py

# Functional tests with FastAPI TestClient
python -c "
from fastapi.testclient import TestClient
import patchpilot
with TestClient(patchpilot.app) as client:
    assert client.get('/').status_code == 200
    assert client.get('/health').status_code == 200
    assert client.get('/jobs').status_code == 200
    print('All tests passed')
"
```

### Guidelines

- Keep the single-file architecture — all code in `patchpilot.py`
- Add Pydantic models for any new request/response types
- Maintain sanitized error messages (no tracebacks to clients)
- Test with `py_compile` and `ast.parse` before committing
- Document new env vars in both the module docstring and this README

---

## License

This project is open source. Matchering 2.0 is licensed under the GNU General Public License v3.0 — see the [Matchering repository](https://github.com/sergree/matchering) for details.

---

<p align="center">
  <strong>PatchPilot</strong> — Created by KrypticKode007 with Maestro AI
</p>
