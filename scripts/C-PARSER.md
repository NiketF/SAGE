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

Ship the resulting `_mft_fast.cp311-win_amd64.pyd` beside mft.py for Python 3.11
x64. Build separately for each supported Python version and architecture; never
label this artifact as an architecture-independent Python wheel. Frozen app
builds must include this optional import explicitly and test the final package.
End users do not need a compiler, headers, Rust or build tools.

## Measure in the working app

```powershell
python -B .\run_sage.py --scan-timings --mft-parser=c
```

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
