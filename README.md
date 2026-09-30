# SAGE

SAGE (Storage Analysis and Guidance Engine) is a Windows-only NTFS storage
prototype. It renders the MFT hierarchy first, then gathers logical and
allocated file sizes in a separate background phase. The right pane answers
only from the current scan; it uses no API key or external AI service.

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
- `services/evidence.py` produces deterministic answers with evidence labels.
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
  deletion workflows, and LLM integration are intentionally out of scope.
- Live filesystems can change during a scan, so a small number of unreadable
  paths is expected and reported rather than treated as a fatal error.
