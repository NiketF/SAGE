"""Time the MFT scan with the Rust and Python backends and check they agree.

Run from an elevated (Administrator) terminal in the project directory:

    $env:PYTHONPATH = 'src'
    python scripts/benchmark_scan.py C:
    python scripts/benchmark_scan.py C: --backend native --repeat 3
"""

from __future__ import annotations

import argparse
import gc
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sage.platform.windows import mft  # noqa: E402
from sage.platform.windows.drives import get_volume_info  # noqa: E402
from sage.services.hierarchy import build_scan_result, calculate_directory_sizes  # noqa: E402


def run(drive: str, backend: str) -> tuple[dict, float, float]:
    os.environ["SAGE_SCANNER"] = backend
    gc.collect()
    started = time.perf_counter()
    records, skipped = mft.enumerate_mft(drive)
    enumerated = time.perf_counter()
    result = build_scan_result(get_volume_info(drive), records, skipped)
    calculate_directory_sizes(result)
    finished = time.perf_counter()
    return records, enumerated - started, finished - enumerated


def summary(records: dict) -> tuple[int, int, int]:
    logical = sum(r.logical_size for r in records.values() if not r.is_directory)
    allocated = sum(r.allocated_size for r in records.values())
    return len(records), logical, allocated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("drive", nargs="?", default="C:")
    parser.add_argument("--backend", choices=("both", "native", "python"), default="both")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()

    backends = ["native", "python"] if args.backend == "both" else [args.backend]
    if "native" in backends and mft._native is None:
        print("sage_native is not installed; build it with: python -m pip install ./native")
        backends.remove("native")
        if not backends:
            return 1

    summaries = {}
    for backend in backends:
        for attempt in range(1, args.repeat + 1):
            records, scan_s, tree_s = run(args.drive, backend)
            summaries[backend] = summary(records)
            count, logical, allocated = summaries[backend]
            print(f"{backend:>6} run {attempt}: MFT read+parse {scan_s:7.2f} s | "
                  f"tree+sizes {tree_s:6.2f} s | total {scan_s + tree_s:7.2f} s | "
                  f"{count:,} records, {logical / 1e9:,.1f} GB logical, {allocated / 1e9:,.1f} GB allocated")
            del records
    if len(summaries) == 2:
        # Live volumes change between runs, so small differences are expected.
        native, python = summaries["native"], summaries["python"]
        print(f"record difference: {native[0] - python[0]:+,}; "
              f"allocated difference: {(native[2] - python[2]) / 1e6:+,.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
