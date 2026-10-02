# SAGE

SAGE (Storage Analysis and Guidance Engine) is a Windows-only NTFS storage
prototype. It renders the MFT hierarchy first, then gathers logical and
allocated file sizes in a separate background phase. The right pane uses a
local model (`qwen3.5:4b`) for conversational, scan-grounded answers. No API
key or cloud service is required.

The prominent bar beneath the scan controls shows estimated MFT progress,
tree-building activity, then exact file-size progress. Folder and file icons distinguish items in the tree.
The size columns update as evidence arrives; a `+` suffix means the displayed
folder total is a lower bound until the size pass finishes.

## Requirements

- Windows 10 or 11
- Python 3.11 or newer
- An NTFS fixed drive
- Administrator approval for raw volume access

The runtime and automated tests use only the Python standard library.
Conversational answers additionally need Ollama running locally with
`qwen3.5:4b` downloaded. Scanning and basic evidence answers still work if
Ollama is unavailable.

## Local AI

Install Ollama separately and download the model with `ollama pull qwen3.5:4b`.
SAGE connects only to `127.0.0.1:11434`; it does not send scan data to a cloud
API. Questions run in a background worker so the tree remains responsive.
The Ollama application/server must be running; SAGE does not start it.
You do not need a separate `ollama run` chat session. SAGE allows up to 180
seconds per model request and requests that the model stay loaded for 10 minutes
between questions. SAGE starts loading the model in a background worker when
the app opens, so loading can overlap scanning. Cold loading and partial CPU
execution can still take time.

Greetings and simple count/total-size questions answer immediately from local
logic. Other questions first resolve mentioned folder/file names in the current
scan, then send only relevant evidence and recent conversation to one model
request. A folder-name index and cached largest-item rankings avoid repeated
work. Duplicate names are supplied as multiple paths for clarification.
Retrieval uses the existing in-memory scan, not another MFT pass or file reads.
The model receives no file-operation tools or file contents.

Replies stream into the assistant pane as text arrives. Basic Markdown bold,
italics, headings, bullets, and inline code render with Tk text styles. A spinner
shows actual retrieval/generation stages and elapsed time. Scanning again
invalidates old assistant events.

The model uses a 2,048-token context and a 240-token response cap to reduce
latency and memory demand. Conversation history is bounded. Larger explanations
can require a follow-up; instant model answers are not guaranteed on CPU or a
GPU that cannot hold the whole model.

Run the read-only smoke evaluation on a synthetic scan with:

```powershell
$env:PYTHONPATH = "src"
python scripts/evaluate_local_ai.py
```

Measure retrieval separately from the model with synthetic data:

```powershell
$env:PYTHONPATH = "src"
python scripts/benchmark_local_ai.py --records 600000
python scripts/benchmark_local_ai.py --records 600000 --live
```

The live benchmark reports model loading, first text, and completion times.
It changes no files on the scanned drive.

SAGE marks Windows and application locations as protected for cleanup advice.
The model cannot know every application dependency; unknown paths are review
candidates, not confirmed safe deletions. Tree file actions remain separate
from AI and require direct user interaction.
Model replies can contain errors, including incorrect size conversions. Check
the measured sizes in the tree before acting on a cleanup suggestion.

## Run from source

The quickest route needs no installation:

```powershell
python run_sage.py
```

To install the optional `sage` command instead:

```powershell
python -m pip install -e .
sage
```

SAGE requests UAC elevation on launch. A cancelled UAC prompt ends the launch.

## Validate

```powershell
python -m compileall -q src tests
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

## Architecture

- `platform/windows/mft.py` performs native `FSCTL_ENUM_USN_DATA` enumeration.
- `platform/windows/file_operations.py` handles Windows Shell file operations.
- `services/hierarchy.py` reconstructs parents and aggregates counts in memory.
- `platform/windows/sizes.py` batches file size and allocation metadata by
  directory, with exact per-file queries only for entries that could not be batched.
- `services/evidence.py` produces deterministic fallback answers.
- `services/retrieval.py` retrieves bounded scan evidence and cleanup-risk labels.
- `services/local_ai.py` handles fast replies, relevant evidence, bounded history,
  and streaming from the local model.
- `ui/markdown.py` converts assistant formatting into native Tk text styles.
- `ui/` contains the two-pane Tkinter interface and background-job coordinator.

The scan coordinator assigns every scan a generation number and cancels older
workers. UI updates travel through a queue owned by the Tk main thread, so stale
results from a previous drive cannot replace a newer scan.

## Working with files

Right-click a file or folder in the left tree. SAGE can open it, reveal it in
Explorer, copy its path, copy or cut and paste within the tree, copy or move it
to a chosen folder, rename it, or send it to the Recycle Bin. Keyboard shortcuts
include Ctrl+C, Ctrl+X, Ctrl+V, F2, and Delete. File changes trigger a fresh
scan so the displayed evidence stays current. The drive root cannot be moved,
renamed, or recycled from the tree. Recycle actions require confirmation.

## Prototype boundaries

- The hierarchy scan is MFT-backed and never falls back to `os.walk()`.
- Size collection queries many files at once through Windows directory metadata,
  with four concurrent workers and a bounded task queue. Inaccessible or missing
  entries use per-file fallback. Very large volumes can still take time; this is
  not a direct bulk `$MFT` size parser. Partial folder sizes are marked until completion.
- Folder allocation totals sum contained files. NTFS directory metadata overhead
  is not included.
- Active MFT records are analyzed. Deleted-record forensics, alternate data
  streams, hard-link name expansion, SQLite persistence, duplicate detection,
  automated deletion workflows, and file-content indexing are out of scope.
- Live filesystems can change during a scan, so a small number of unreadable
  paths is expected and reported rather than treated as a fatal error.
