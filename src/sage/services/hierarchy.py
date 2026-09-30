"""Build and aggregate the MFT parent/child hierarchy."""

from __future__ import annotations

from collections import Counter, defaultdict

from ..config import FILE_ATTRIBUTE_DIRECTORY
from ..models import FileRecord, ScanResult, VolumeInfo

NTFS_ROOT_FRN = 5
NTFS_RECORD_NUMBER_MASK = (1 << 48) - 1


def find_root(records: dict[int, FileRecord]) -> int:
    """Find NTFS MFT entry 5 while preserving its full sequence-tagged ID."""
    for record in records.values():
        if record.is_directory and record.frn & NTFS_RECORD_NUMBER_MASK == NTFS_ROOT_FRN:
            return record.frn

    # USN enumeration may omit the root itself. Its children still identify it
    # by the full parent reference, including the sequence number.
    root_parents = Counter(
        record.parent_frn
        for record in records.values()
        if record.parent_frn & NTFS_RECORD_NUMBER_MASK == NTFS_ROOT_FRN
    )
    if root_parents:
        root_frn, _ = root_parents.most_common(1)[0]
        records[root_frn] = FileRecord(
            root_frn, root_frn, ".", FILE_ATTRIBUTE_DIRECTORY
        )
        return root_frn

    raise ValueError(
        "Could not identify the NTFS root (MFT entry 5) from "
        f"{len(records):,} returned records."
    )


def build_scan_result(
    volume: VolumeInfo,
    records: dict[int, FileRecord],
    skipped_records: int = 0,
) -> ScanResult:
    root_frn = find_root(records)
    children: defaultdict[int, list[int]] = defaultdict(list)

    for record in records.values():
        if record.frn == root_frn:
            continue
        parent = records.get(record.parent_frn)
        if parent is None or not parent.is_directory or record.parent_frn == record.frn:
            record.parent_frn = root_frn
        children[record.parent_frn].append(record.frn)

    result = ScanResult(
        volume=volume,
        records=records,
        children=dict(children),
        root_frn=root_frn,
        skipped_records=skipped_records,
    )
    calculate_stats(result)
    return result


def _directory_postorder(result: ScanResult) -> list[int]:
    order: list[int] = []
    stack: list[tuple[int, bool]] = [(result.root_frn, False)]
    entered: set[int] = set()

    while stack:
        frn, expanded = stack.pop()
        if expanded:
            order.append(frn)
            continue
        if frn in entered:
            continue
        entered.add(frn)
        stack.append((frn, True))
        for child_frn in result.children.get(frn, ()):
            child = result.records.get(child_frn)
            if child and child.is_directory:
                stack.append((child_frn, False))
    return order


def calculate_stats(result: ScanResult) -> dict[int, tuple[int, int, int]]:
    """Map each directory to recursive (items, files, folders) counts."""
    totals: dict[int, tuple[int, int, int]] = {}
    for frn in _directory_postorder(result):
        files = 0
        folders = 0
        for child_frn in result.children.get(frn, ()):
            child = result.records.get(child_frn)
            if child is None:
                continue
            if child.is_directory:
                child_items, child_files, child_folders = totals.get(child_frn, (0, 0, 0))
                files += child_files
                folders += child_folders + 1
            else:
                files += 1
        totals[frn] = (files + folders, files, folders)
    result.stats = totals
    return totals


def calculate_directory_sizes(result: ScanResult) -> dict[int, tuple[int, int]]:
    """Aggregate file sizes bottom-up without revisiting the filesystem."""
    totals: dict[int, tuple[int, int]] = {}
    for frn in _directory_postorder(result):
        logical = 0
        allocated = 0
        for child_frn in result.children.get(frn, ()):
            child = result.records.get(child_frn)
            if child is None:
                continue
            if child.is_directory:
                child_logical, child_allocated = totals.get(child_frn, (0, 0))
                logical += child_logical
                allocated += child_allocated
            else:
                logical += child.logical_size
                allocated += child.allocated_size
        totals[frn] = (logical, allocated)
    result.directory_sizes = totals
    result.sizes_complete = True
    return totals
