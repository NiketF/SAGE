"""Public scan orchestration for the fast MFT phase and size phase."""

from __future__ import annotations

from collections.abc import Callable
import time

from ..models import ScanResult
from ..platform.windows.drives import get_volume_info
from ..platform.windows.mft import enumerate_mft
from ..platform.windows.sizes import collect_file_sizes
from .hierarchy import build_scan_result, calculate_directory_sizes
from .scan_timing import begin_scan_timings, write_scan_timings


def scan_ntfs(
    drive: str,
    progress: Callable[[int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> ScanResult:
    """Complete only the fast MFT hierarchy phase."""
    volume = get_volume_info(drive)
    if volume.filesystem.upper() != "NTFS":
        raise ValueError(f"{volume.drive} is {volume.filesystem}, not NTFS.")
    timings = begin_scan_timings(progress, volume.drive)
    scan_started = time.perf_counter()
    try:
        records, skipped = enumerate_mft(
            volume.drive,
            progress=progress,
            cancelled=cancelled,
        )
    except BaseException as exc:
        if timings is not None:
            timings.update(status='cancelled' if isinstance(exc, InterruptedError) else 'failed',
                           error_type=type(exc).__name__)
            write_scan_timings(timings)
        raise
    if timings is not None:
        timings['enumeration_seconds'] = time.perf_counter() - scan_started
    stage = getattr(progress, "on_stage", None)
    if stage:
        stage("Building MFT hierarchy")
    readiness_started = time.perf_counter()
    # Single generator walk instead of list materialization + second iteration.
    # Still O(N) in the common all-True case, but roughly halves this phase's cost.
    sizes_ready = all(r.size_ready for r in records.values() if not r.is_directory)
    if timings is not None:
        timings['file_readiness_seconds'] = time.perf_counter() - readiness_started
    hierarchy_started = time.perf_counter()
    result = build_scan_result(volume, records, skipped, include_sizes=sizes_ready)
    hierarchy_seconds = time.perf_counter() - hierarchy_started
    if sizes_ready:
        # Non-directory count is already produced by calculate_stats as stats[root][1].
        root_totals = result.stats.get(result.root_frn)
        result.sized_files = root_totals[1] if root_totals is not None else 0
    if timings is not None:
        timings.update(hierarchy_and_counts_seconds=hierarchy_seconds,
                       hierarchy_and_totals_seconds=hierarchy_seconds if sizes_ready else None,
                       aggregation_mode='combined' if sizes_ready else 'counts_only_pending_sizes',
                       folder_sizes_seconds=0.0,
                       scanner_total_seconds=time.perf_counter() - scan_started,
                       records=len(result.records), skipped_records=skipped,
                       status='scanner_complete')
        write_scan_timings(timings)
    return result


def analyze_sizes(
    result: ScanResult,
    progress: Callable[[int, int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Aggregate folder totals; walk directories only when MFT sizes are missing."""
    files = [record for record in result.records.values() if not record.is_directory]
    if not files or all(record.size_ready for record in files):
        result.sized_files = len(files)
        calculate_directory_sizes(result)
        if progress:
            progress(len(files), len(files), result.size_errors)
        return
    collect_file_sizes(result, progress, cancelled)
    if cancelled and cancelled():
        raise InterruptedError("Size analysis cancelled.")
    calculate_directory_sizes(result)
