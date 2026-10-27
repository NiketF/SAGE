# Measure the existing MFT scanner

The optional Rust scanner has been retired. SAGE uses its original Python MFT
reader, hierarchy and size calculations. The optional C batch parser accelerates
only record parsing. The tree's behavior and AI code are unchanged.

Run `run_sage_timed.ps1` from the repository and perform a normal C: scan. The
launcher passes `--scan-timings --mft-parser=c`, which enables diagnostics and
requires the compiled parser in the app after UAC
relaunch as well. Startup logging records whether diagnostics are enabled.
Ordinary startup does not write timing reports.

The latest local report is `%LOCALAPPDATA%\SAGE\scan-timing-last.json`. It contains
the backend, drive letter, record count, status and durations, not filenames or
file contents. Writes are atomic; a reporting failure does not fail scanning.

Durations are seconds. `enumeration_seconds` includes disk reading, record parsing,
extension/hard-link reconciliation, callbacks and other enumeration overhead.
`hierarchy_and_counts_seconds` and `folder_sizes_seconds` follow enumeration.
`scanner_total_seconds` includes these stages. Do not add overlapping totals.
`tree_insert_seconds` measures the existing TreePanel call. Cleanup calculation
runs separately. `cleanup_compute_seconds` measures calculation and
`cleanup_buttons_seconds` measures UI publication, after any active AI reply.

`scan_to_first_idle_seconds` starts at the existing Scan button timestamp and ends
when Tk next reaches an idle callback after scan completion handling. It is a
responsiveness proxy, not a direct screen-paint measurement. Background AI indexing
may still be running. An old scan's idle callback cannot overwrite a newer scan.

`status=tree_ready` means the tree reached the idle callback while cleanup was
still running. `status=complete` includes cleanup publication on C:;
`scanner_complete` means only the scanner has finished. Failed or cancelled raw
enumeration is reported separately. A fallback size pass is not covered by this
first timing report and must not be presented as fully measured.

`parser_backend` confirms `c` or `python`; `backend=python_mft` identifies the drive
reader, not the parser. Real-app measurements decide the actual speed gain.
