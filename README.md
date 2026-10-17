# SAGE

SAGE (Storage Analysis and Guidance Engine) is a Windows-only NTFS storage
prototype. It reads `$MFT` sequentially from the volume (the same approach as
WizTree): names, parents, and logical/allocated sizes come from one pass, so
the tree is usable with sizes as soon as the MFT scan finishes. After that
scan of C:, the assistant retains its specific rule-matched cleanup list,
with manual confirmation buttons in the chat. The right pane also uses a
local model (`qwen3.5:4b`) for conversational, scan-grounded questions. No API key or
cloud service is required.

The prominent bar beneath the scan controls shows estimated `$MFT` read
progress, then tree-building activity. Folder and file icons distinguish items
in the tree. Size columns are filled from the MFT records; a directory-walk
size pass runs only if a record had no `$DATA` size.

## Requirements

- Windows 10 or 11
- Python 3.11 or newer
- An NTFS fixed drive
- Administrator approval for raw volume access

The runtime and automated tests use only the Python standard library.
Conversational answers additionally need Ollama running locally with
`qwen3.5:4b` downloaded. Scanning and basic evidence answers still work if
Ollama is unavailable.

## Cleanup suggestions and permanent delete

When a scan finishes, the assistant lists specific items that are safe to
delete on that drive, with exact paths and sizes, largest first. Only items
over 20 MB that match known rules are listed: Windows Update and Delivery
Optimization caches, Windows and user temp folders, crash dumps and error
reports, browser and Electron app caches (Chrome, Edge, Brave, Firefox,
Discord, Teams, VS Code, â€¦), GPU shader caches, pip/npm/Yarn/NuGet/Gradle/Go
caches, project `node_modules` folders (only beside a `package.json`, never
inside installed apps, `AppData`, or `resources` folders), installers (`.exe`, `.msi`, `.msix`, `.iso`) in
Downloads, the Recycle Bin, and `Windows.old` upgrade leftovers. Documents,
photos, Program Files, and generic AppData folders are not listed. Nested
matches are collapsed so the same space is never counted twice.

Each item has a button directly in the chat. Pressing it asks for confirmation,
then deletes **permanently** (no Recycle Bin). Cache folders keep the folder and
delete its contents; files that are in use are skipped and reported. The
Recycle Bin item empties the bin through Windows. The drive is rescanned
afterward. Delete buttons only ever come from these rules; a model reply can
add a button only for a path that is already in the verified list.

The existing C: cleanup list and its manual confirmation buttons are retained.
The conversational assistant can inspect these candidates and explain their
conditions, but it never creates delete buttons or performs file operations.
Questions are interpreted by the model using the selection, recent conversation,
and validated evidence tools, rather than a phrase-specific reply dispatcher.

The conversation can review files, folders, and subfolders beyond rule-matched
caches. Finding no rule candidate does not mean nothing can be reviewed or
removed. Personal files need review; application and project dependencies need
evidence. Advice does not create a delete button.

SAGE marks Windows and application locations as protected for cleanup advice,
except the specific caches in the verified cleanup list. Unknown paths are
review candidates, not confirmed safe deletions. Every deletion requires the
user to press a button and confirm.

## Local conversational AI

The UI uses `services/conversation.py` (`DriveAssistant`). Qwen interprets the
question with recent conversation, selected items, and previous results. It can
choose validated read-only tools to inspect a path, search names or metadata,
list children, search approved text, find references, and identify possible
duplicates. There is no phrase-specific dispatcher in this UI conversation path.
The older `local_ai.ConversationAssistant` remains for legacy callers and tests.

Qwen writes the description and assessment in its own words; SAGE places the
description first without supplying canned role sentences. SAGE supplies exact sizes,
counts, and modification dates through `report_fact`; the model chooses that
operation from the meaning of the question. Advice requests continue through
the explanation flow rather than stopping at a supporting measurement.
Ambiguous subjects should receive a question, not an assumed path.

Selected and explicitly named items receive a bounded tree inspection before
the model answers. This includes nested file-type counts, example file and
subfolder paths, retained attributes, unknown measurements and scan totals.
It uses the retained MFT tree without opening descendant files. Reparse-point
targets are not followed. A file also receives its surrounding folder context.
Search-discovered items are inspected before the next answer (up to four per
result, with incomplete coverage marked). This does not require another model
call. The same evidence supports deletion, editing, updates, replacement,
moving, renaming and other change questions; the model interprets the action.

Tree work is bounded to 20,000 records or approximately 100 milliseconds per
item and cached for repeated questions. Large trees, links and inconsistent
records are explicitly marked partial. Summary samples are not a complete
file listing, and inspection cannot establish live use, backups, application
dependencies, effective permissions or deletion safety. The response instructions
require purpose first, action-specific consequences and an appropriate next step
or alternative. Model-written reasoning still needs quality evaluation.

Ollama and `qwen3.5:4b` must be installed separately. All model communication
uses `127.0.0.1:11434`. Drive data and the SQLite index remain local. No cloud
API, API key, new runtime package, or model training is required.

### Scan and indexing

The tree is displayed without waiting for SQLite or content indexing. Indexing
runs in a cancellable background worker with progress. It replaces removed scan
paths, reuses unchanged excerpts, and closes database connections explicitly.
Searches can use bounded current-scan metadata while indexing is still running.

Approved text excerpts have a 128 KiB file limit and at most eight 500-character
chunks per file. Background text work has a two-second budget per scan update;
this is partial coverage, not an exhaustive content audit. Sensitive locations,
unapproved formats, binary files, offline items, and linked paths are excluded.
The indexing API supports include/exclude paths. It never executes a file.
If indexed text has no match, a targeted search can check up to 64 approved small
files within a further one-second budget. This also has partial coverage.
Item evidence includes nearby parent and sibling names from the scan to help
explain context; nearby names do not confirm application ownership.
PDF and Office content adapters and embeddings are not included.

Content hits describe indexed text, not proof that a program currently uses a
file. A missing hit does not prove a file is unreferenced. Duplicate candidates
share a measured size; their contents are not compared. These limits remain in
the tool evidence and response safeguards.

### Responsiveness and safety

The model is prepared in the background and kept warm for 30 minutes. A cold
or unavailable model returns useful scan facts rather than queuing a long
answer. Fifteen seconds is a performance target, not a cancellation point.
There is a 30-second request ceiling, short adaptive output limits, at most two
tool rounds, and no extended reasoning mode. Local disk access can still vary
in duration. The HTTP deadline disconnects slow streams, and rescan cancels the
old conversation's active connection.

Complete sentences stream into the panel after path checks. The final answer replaces
the provisional stream. Replies cut by the output limit are labelled partial,
with an option to continue the conversation. On a small model, a partial answer
may omit some requested explanation; it is not a verified full assessment.

The new conversation cannot perform delete, move, rename, or modification
operations and creates no operation buttons. The separate C: policy remains
extensible; the existing C: suggestion list and manual tree operations retain
their established safeguards. Whole-C-drive cleanup advice uses existing rule
candidates. The model does not receive file-changing tools.

Language targets short, direct, STE-inspired sentences. This is not formal
ASD-STE100 certification. Model-written explanations still require evaluation;
path checks and read-only tools cannot prove every natural-language conclusion.

Run current response checks and local-model trials from the project directory:

```powershell
$env:PYTHONPATH = 'src;tests'
python -B -m unittest test_drive_conversation -v
python -B scripts/evaluate_drive_conversation.py --warm --output conversation-review
```

The trial uses synthetic scan records and synthetic approved text. It does not
scan personal files. JSON preserves timings and tool results; RESPONSES.md
shows actual replies and marks partial/fallback outcomes. The older
`evaluate_local_ai.py` and `benchmark_local_ai.py` exercise the legacy service.

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

- `platform/windows/mft.py` reads `$MFT` from the raw volume, applies the
  NTFS update-sequence fixup, and parses FILE records for names, parents,
  unnamed `$DATA` sizes, modified time, and attributes. Only junctions and
  symlinks keep the link attribute; OneDrive placeholders and compressed files
  do not.
- `platform/windows/file_operations.py` handles Windows Shell copy/move/rename
  and Recycle Bin actions, plus permanent delete and emptying the Recycle Bin
  from chat cleanup buttons.
- `services/hierarchy.py` reconstructs parents and aggregates counts and folder
  totals in memory.
- `platform/windows/sizes.py` is the fallback directory-walk size collector,
  used only when MFT records did not already supply sizes.
- `services/cleanup.py` holds the cleanup rules and builds the specific
  safe-to-delete list from the finished scan.
- `services/evidence.py` produces deterministic fallback answers.
- `services/retrieval.py` retrieves bounded scan evidence and cleanup-risk labels.
- `services/local_database.py` stores the persistent local SQLite metadata/content
  index used by conversational retrieval.
- `services/tool_registry.py` validates and executes approved read-only assistant
  tools (`search_names`, `search_content`, `inspect_path`, `list_children`,
  `summarize_folder`, `find_large_files`, `retrieve_context`, `impact_analysis`).
- `services/action_policy.py` applies drive-aware read-only action policy
  (including stricter `C:` policy tier).
- `services/profile.py` builds the per-item profile: scan metadata, owning
  installed app, program version information, and download source.
- `services/known_folders.py` is the knowledge list of well-known Windows
  locations and their supported cleanup routes.
- `services/conversation.py` is the active model-led UI conversation, bounded
  retrieval loop, memory, response checks, and streaming decoder.
- `services/local_ai.py` contains the deadline/cancellation-aware Ollama client
  and the earlier conversation service for compatibility.
- `ui/assistant_panel.py` renders chat replies and embeds a delete button per
  verified cleanup item.
- `ui/markdown.py` converts assistant formatting into native Tk text styles.
- `ui/` contains the two-pane Tkinter interface and background-job coordinator.

The scan coordinator assigns every scan a generation number and cancels older
workers. UI updates travel through a queue owned by the Tk main thread, so stale
results from a previous drive cannot replace a newer scan.

## Working with files

Right-click a file or folder in the left tree. SAGE can open it, reveal it in
Explorer, copy its path, copy or cut and paste within the tree, copy or move it
to a chosen folder, rename it, or send it to the Recycle Bin. Keyboard shortcuts
include Ctrl+C, Ctrl+X, Ctrl+V, F2, and Delete. Verified cleanup items in the
assistant pane can be deleted permanently after confirmation. File changes
trigger a fresh scan so the displayed evidence stays current. The drive root
cannot be moved, renamed, or recycled from the tree. Recycle and permanent
delete actions require confirmation.

## Prototype boundaries

- The hierarchy scan is MFT-backed and never falls back to `os.walk()`.
- File sizes are taken from unnamed `$DATA` on each in-use MFT record during
  the sequential `$MFT` read. Folder totals are summed in memory afterward.
  A Windows directory metadata walk remains only as fallback for records that
  had no MFT size. Scanning is I/O-bound on `$MFT` (typically hundreds of MB),
  not on opening every folder on the volume. Python is still slower than a
  native C++ tool such as WizTree, but it no longer scales with directory-walk
  syscalls. `$MFT` mapping pairs are read from record 0; volumes that store
  those runs only in `$ATTRIBUTE_LIST` are not handled.
- Folder allocation totals sum contained files. NTFS directory metadata overhead
  is not included.
- Active MFT records are analyzed. Deleted-record forensics, alternate data
  streams, hard-link name expansion, and content-verified duplicate detection are still out of
  scope. Conversational indexing is local-only, read-only, and bounded by file
  type, size limits, and safety exclusions.
- Live filesystems can change during a scan, so a small number of unreadable
  paths is expected and reported rather than treated as a fatal error.
