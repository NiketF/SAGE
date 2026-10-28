# Compiled block parser

The C parser is a CPython extension with no third-party runtime packages. It
receives existing MFT bytearrays in blocks, performs the same record fixups and
attribute parsing, and creates existing FileRecord objects directly. The original
reader still owns drive access, cancellation and progress. Python still owns
hard-link/extension reconciliation, hierarchy, size aggregation, the tree and AI.

Optimization revision 2 avoids building Python name/stream containers for the
common single-name/single-stream case. It materializes the exact same dictionaries
when links, extension records, attribute lists or multiple streams need them.
The reader now supplies an owned writable bytearray, avoiding a bytes copy and
another full-block copy. It releases the native buffer view before resizing,
and retains the existing alignment, short-read and record-carry behavior.
The timing report identifies `parser_optimization_version=2` and
`read_buffer_mode=owned_bytearray`. Hierarchy calculations use the same directory
attribute flag directly and reuse local dictionary lookups; their output is unchanged.

The scan orchestration also avoids keeping a temporary list of every file while
building the hierarchy. Counts and ready sizes are aggregated with one visit to
each file child, followed by folder-only aggregation. An iterative stack and an
active-ancestor set preserve cycle handling without recursion or a whole-tree
visited set retained during aggregation. Hard-link reconciliation uses one
ordered deduplication map, preserving primary paths, alias order and allocation.
Those Python-only improvements apply to both parser paths and do not require a
new binary. The native revision below requires the accompanying rebuilt binary.

Optimization revision 3 of the native parser additionally shares the last parent,
modification time and allocated-size integer within each block. The cache holds
at most three immutable integers and is cleared on success or failure. Values,
record objects and cross-block results are unchanged. Lower-ranked DOS spellings
are not decoded when they cannot be selected or retained as aliases; DOS-only
records and all useful hard-link names remain supported. The parser uses ordinary
x64 build settings without a machine-specific instruction-set requirement.

Large scans (at least two configured read blocks) now use one read-ahead worker
by default. The next block starts reading while the current block is parsed;
there is at most one pending read. Small scans retain sequential reading. The
same aligned reader, volume handle, run boundaries and short-read handling are
used. Cancellation and errors join the reader before closing the volume handle;
an in-flight synchronous Windows read can still delay cancellation.

`read_pipeline=one_block_ahead` identifies this mode in scan timings. Read and
parse durations can overlap and must not be summed to obtain total scan time.
Worker read duration can include scheduling/GIL delays. Use scanner total and
time until the interface is usable to compare runs. To compare sequential
reading on the same build, add `--mft-read-ahead=off`; use `on` to force read-ahead
for small test scans, or `auto` for the default size threshold. These options
survive the existing administrator restart. No index or AI behaviour is changed.

The timing report identifies this aggregation with
`aggregation_algorithm=single_child_pass`. It also records MFT valid bytes,
record size, data-run count, read-call count, actual bytes returned by reads,
base-record count, records with extra attributes, and extension-group count.
Read bytes include the separate bootstrap read of MFT record 0; they can exceed
valid bytes. These are workload measurements, not filenames or file contents.
Use them to compare slow system-volume scans across machines. CPU-only synthetic
comparisons do not establish disk-read or end-to-end scan speed on other PCs.

The existing Python record parser remains available. `SAGE_MFT_PARSER=auto` is the
default: it uses C when the extension is installed and Python when it is absent.
A broken installed extension raises an error, rather than silently hiding a load
failure. `--mft-parser=python` or `--mft-parser=c` explicitly selects one parser;
the command-line option is preserved across administrator relaunch.

## Build

On Windows x64, use a MinGW-w64 GCC compiler and the headers/import library for
the Python version running SAGE:

```powershell
python scripts/build_mft_parser.py --cc C:\path\to\gcc.exe
$env:PYTHONPATH = 'src;tests'
python -B -m unittest discover -s tests
```

The checkout includes `_mft_fast.cp311-win_amd64.pyd` for CPython 3.11 x64 and
`_mft_fast.cp315-win_amd64.pyd` for CPython 3.15 x64, beside mft.py. Python selects
the matching filename automatically. Both use optimization revision 3.
Build separately for each supported Python version and architecture; never
label this artifact as an architecture-independent Python wheel. Frozen app
builds must include this optional import explicitly and test the final package.
End users do not need a compiler, headers, Rust or build tools.

## Measure in the working app

```powershell
python -B .\run_sage.py --scan-timings --mft-parser=c
```

For current team testing, install standard 64-bit CPython 3.15.2 with Tcl/Tk,
then use `py -3.15 -B .\run_sage.py --scan-timings --mft-parser=c`. The normal
Windows installer includes Tcl/Tk when that feature is selected. Avoid minimal
embeddable/runtime ZIPs and the free-threaded build for this setup.
`py -3.11` remains available for a baseline when that interpreter is installed.
Requiring `c` exposes an
incompatible or missing parser instead of using the slower Python fallback.
The read-ahead improvement also works on the existing Python fallback path.
Other Python versions and architectures require their own matching builds.
Keep both tested `.pyd` files when committing source changes; `.gitignore`
ignores new `.pyd` files, so the new file needs an explicit `git add -f`.
No runtime, installer or release is created by these source compatibility changes.

Compare with `--mft-parser=python` on the same drive. The local timing report must
show the expected `parser_backend`. Compare counts, skipped records and sizes too.
Synthetic parity checks prove the tested parsing cases, not complete NTFS support
or a guaranteed under-10-second drive scan. Existing size-accounting semantics,
including the previously reported allocated-total discrepancy, are preserved.

Cleanup uses the existing rules and confirmation actions. Calculation happens on
a background worker; all Tk calls remain on the main thread. Completion requires
the same scan and assistant, so old results cannot populate a new drive's UI.
Publication waits during active replies or file operations. No file action is
performed by the parser or cleanup worker.
