<p align="center">
  <img src="https://github.com/NiketF/SAGE/blob/main/SAGE_Banner.jpeg?raw=true" alt="SAGE Banner" width="100%">
</p>


**A fast Windows storage explorer with an AI assistant that runs locally.**

Find what occupies your drive. Explore the files behind the numbers. Ask what they do—and what removing them could change.

- Windows 10 or 11
- Python 3.11 or newer; 64-bit CPython 3.15.2 is the current team test target
- An NTFS fixed drive
- Administrator approval for raw volume access

Windows 10 / 11 · NTFS fixed drives · Python + C · Local AI through Ollama

### Team testing from GitHub

Install the standard **64-bit Windows Python 3.15.2** build with Tcl/Tk support
from [python.org](https://www.python.org/downloads/release/python-3152/). Use the
regular build, not the optional free-threaded build. Pull the branch containing
the matching native parser, then run from the repository folder:

```powershell
py -3.15 -B .\run_sage.py --scan-timings --mft-parser=c
```

The included `_mft_fast.cp315-win_amd64.pyd` targets CPython 3.15 x64. The
existing CPython 3.11 x64 parser remains available for comparison with
`py -3.11`. The explicit `c` option reports an incompatible or missing parser
instead of silently using the slower Python fallback. Team members do not need
a compiler when the correct binary is present. See [parser details](scripts/C-PARSER.md).

The complete 247-test suite passed on both test runtimes. This checks the tested
behaviour; actual drive speed and AI answer quality still require running the app.
No runtime is bundled, no installer is produced, and this source change does not
create a published release.

## Cleanup suggestions and permanent delete

---

## Less guessing. More understanding.

A large folder tells you where your storage went. It does not tell you what the folder does or what you would lose by removing it.

**SAGE brings the file tree and the conversation together.** Its Storage Analysis and Guidance Engine reads NTFS metadata, builds a navigable drive tree, and gives the assistant access to evidence from that scan.

- **Find the space:** browse logical sizes, allocated space, file counts and folder totals.
- **Understand the item:** select a file or folder and ask about its role, contents or removal options.
- **Keep the context:** continue the conversation with the selected item, recent questions and retrieved scan evidence.
- **Stay in control:** use explicit file actions and review cleanup suggestions before confirming a destructive operation.

SAGE is an **actively developed source preview**. The download above contains the project source, not a one-click Windows installer.

## The SAGE workflow

### 01 — Scan your drive

Choose an NTFS drive and start a scan. SAGE reads the Master File Table directly instead of opening every folder for its initial scan. A compiled C batch parser handles record processing when available.

The tree appears with the retained file metadata and calculated folder totals. Local search indexing runs in the background; on C:, cleanup suggestions are prepared separately so their calculation does not hold up tree interaction.

### 02 — Explore what occupies the space

Expand folders, change the sort order and inspect individual items. The tree shows file size, attributed disk allocation, item counts and file attributes.

Right-click to open an item, reveal it in Explorer, copy its path, copy or move it, rename it, or send it to the Recycle Bin. Drive roots have additional restrictions.

### 03 — Ask SAGE

Select an item and use **Ask SAGE**, or type a question in the assistant pane:

> Which folders use the most space on this drive?
>
> What is this folder used for?
>
> Can I remove this application, and what features would I lose?
>
> Find large ZIP files on this drive.
>
> What can I clean up on C:?

The assistant can retrieve metadata, inspect bounded parts of the hierarchy and search eligible local text. Follow-up questions use recent conversation context. For repeated filenames, select the exact item or provide its full path.

### 04 — Review, then act

SAGE distinguishes system-managed items, application-removal options, managed cleanup, personal data and items that need more evidence. Explanations aim to cover purpose, consequences, conditions and an appropriate removal method.

C: cleanup suggestions come from existing rules, with paths and sizes. Suggested categories include selected caches, temporary files, crash reports, development caches and some downloaded installers. A location match is a candidate for review—not a universal safety guarantee.

> [!WARNING]
> **Cleanup buttons can delete permanently after confirmation.** They do not send items to the Recycle Bin. Tree recycle actions are separate. Review the exact path and consequences before confirming.

The chat model has read-only tools. It does not independently delete, move or edit your files. File-changing controls remain separate, and file operations trigger a fresh scan to update the displayed evidence.

## Measured performance

**981,208 records processed in a 7.12-second C: scan.**

Measured locally on an **Intel Core i7-7700HQ**, **16 GB RAM**, with C: on a **Samsung SSD**:

- **Scanner completion:** 36.33 → **7.12 seconds** — **80.4% less time**, approximately **5.1× faster**.
- **Scan to first interface idle:** 42.88 → **7.47 seconds** — **82.6% less time**.
- **MFT record parsing:** 26.72 → **2.30 seconds** — approximately **11.6× faster**.
- **Cleanup suggestions ready:** **11.92 seconds** from scan start, calculated in the background.
- **Latest measured scan:** **981,208 records**, with **3,688 skipped records**.

These are individual local measurements, not controlled identical-data benchmarks. Drive contents changed between runs. Results vary with hardware, MFT size, caching and system activity. First interface idle is a responsiveness proxy, not a direct screen-paint measurement. Scan timings are not AI response-time promises.

The current improvements reduce parser allocations and block copies, use a C batch parser, and combine count and size aggregation into one pass. See [parser implementation notes](scripts/C-PARSER.md).

## Quick start

### What you need

- Windows 10 or 11 and an NTFS fixed drive.
- Administrator approval for raw volume access.
- **64-bit Python 3.11** to use the included compiled parser.
- [Ollama for Windows](https://ollama.com/download/windows) and the `qwen3.5:4b` model for conversational AI.

Python 3.11 or newer can run the source application. Other Python versions need a matching compiled-parser build for the fast path; the Python parser remains available. The included binary targets **CPython 3.11 / Windows x64**.

### Get the source and launch

Download and extract the [source ZIP](https://github.com/NiketF/SAGE/archive/refs/heads/main.zip), open PowerShell in the extracted folder, and run:

```powershell
python -B .\run_sage.py
```

Or clone the repository:

```powershell
git clone https://github.com/NiketF/SAGE.git
cd SAGE
python -B .\run_sage.py
```

Approve the administrator prompt, select a drive, and scan. The default parser selection uses the compiled component when it is installed and the Python parser when it is absent. The standard source launch does not require installing a Python package.

### Enable the local AI assistant

Install [Ollama](https://ollama.com/download/windows), then download SAGE's configured model:

```powershell
ollama pull qwen3.5:4b
```

Keep Ollama available and launch SAGE. Model preparation happens in the background. Scanning and deterministic evidence features remain available without conversational AI.

The model download needs internet access. Local inference speed depends on model readiness and CPU/GPU resources. The model is installed separately from SAGE; see the [model page](https://ollama.com/library/qwen3.5).

## Local by design

SAGE's current AI client communicates with Ollama at `127.0.0.1:11434`. There is no integrated cloud AI API or API-key requirement.

The local SQLite index stores scan metadata and limited excerpts from eligible text files. MFT scanning itself does not read every file's contents. Text indexing is bounded by format, size, time and location rules; it is not a complete content audit.

## Know the boundaries

SAGE helps you investigate and decide. It does not establish every application's dependencies or know which personal files matter to you.

- **Scan coverage:** skipped records and live filesystem changes can affect results. A missing search hit is not proof that a file does not exist.
- **Space accounting:** logical size, attributed allocation and recoverable space differ. Allocated totals still need validation on some volumes; a scan total is not a promise of space that deletion will recover.
- **Hard links:** multiple paths can share physical storage. Removing one path may free no file data.
- **Duplicate candidates:** equal measured sizes identify possibilities. File contents are not compared to prove duplicates.
- **AI coverage:** retrieved hierarchy samples and text searches can be partial. PDF/Office content adapters and embeddings are not included. Treat explanations as assistance, not permission to delete.
- **NTFS support:** this preview does not provide deleted-file forensics or network/non-NTFS scanning. MFT layouts requiring additional mapping records beyond the supported root mapping are rejected rather than reported as complete.

<details>
<summary><strong>Under the hood: the scan and assistant pipeline</strong></summary>

```text
NTFS drive
  → Existing raw MFT reader
  → C batch parser, or Python compatibility parser
  → Hard-link and extension-record reconciliation
  → Parent–child hierarchy + combined counts and sizes
  → Interactive file tree
      ├─ Background local search indexing
      ├─ Background C: cleanup preparation
      └─ Selected item / question
          → Validated read-only evidence tools
          → Local Ollama model
          → Streamed explanation
```

Python coordinates the application and retains the existing `ScanResult` contract. The C extension accelerates record processing. Tk updates stay on the main thread; worker results are checked against the active scan so stale results cannot replace a newer drive's view.

Relevant code: [MFT reader](src/sage/platform/windows/mft.py), [C parser](src/sage/platform/windows/_mft_fast.c), [hierarchy](src/sage/services/hierarchy.py), [conversation](src/sage/services/conversation.py), [tool validation](src/sage/services/tool_registry.py), and [cleanup rules](src/sage/services/cleanup.py).

</details>

<details>
<summary><strong>Development, validation and timing</strong></summary>

Run the checks from the repository root:

```powershell
$env:PYTHONPATH = 'src;tests'
python -B -m unittest discover -s tests
```

The latest optimization was checked with **233 automated tests**. Compiled-parser checks need the matching extension installed. Automated checks support, rather than replace, validation in the real application.

Capture a local timing report with an explicit parser selection:

```powershell
python -B .\run_sage.py --scan-timings --mft-parser=c
```

Use `--mft-parser=python` for comparison. Timings are saved locally to `%LOCALAPPDATA%\SAGE\scan-timing-last.json`. Check the active parser, record count and skipped records alongside duration. See [timing definitions](scripts/SCAN-TIMING.md) and [compiled-parser build instructions](scripts/C-PARSER.md).

</details>

## Help improve SAGE

Found a scan mismatch, an unclear answer or a performance issue? [Open an issue](https://github.com/NiketF/SAGE/issues) with your Windows version, app revision, parser selection and anonymized timings. For AI issues, include a reproducible question and expected behavior without sharing private files.

---

**See the space. Ask the question. Make an informed choice.**

[**Get the SAGE source**](https://github.com/NiketF/SAGE/archive/refs/heads/main.zip) · [Explore the code](https://github.com/NiketF/SAGE) · [Report an issue](https://github.com/NiketF/SAGE/issues)

