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
    *,
    include_sizes: bool = False,
) -> ScanResult:
    root_frn = find_root(records)
    children: defaultdict[int, list[int]] = defaultdict(list)

    for record in records.values():
        if record.frn == root_frn:
            continue
        parent = records.get(record.parent_frn)
        if parent is None or not parent.attributes & FILE_ATTRIBUTE_DIRECTORY or record.parent_frn == record.frn:
            record.parent_frn = root_frn
        children[record.parent_frn].append(record.frn)

    result = ScanResult(
        volume=volume,
        records=records,
        children=dict(children),
        root_frn=root_frn,
        skipped_records=skipped_records,
    )
    calculate_stats(result, include_sizes=include_sizes)
    return result


def _directory_postorder(result: ScanResult) -> list[int]:
    order: list[int] = []
    stack: list[tuple[int, bool]] = [(result.root_frn, False)]
    entered: set[int] = set()
    get_record = result.records.get
    get_children = result.children.get

    while stack:
        frn, expanded = stack.pop()
        if expanded:
            order.append(frn)
            continue
        if frn in entered:
            continue
        entered.add(frn)
        stack.append((frn, True))
        for child_frn in get_children(frn, ()):
            child = get_record(child_frn)
            if child and child.attributes & FILE_ATTRIBUTE_DIRECTORY:
                stack.append((child_frn, False))
    return order


def calculate_stats(result: ScanResult, *, include_sizes: bool = False) -> dict[int, tuple[int, int, int]]:
    """Aggregate counts, and optionally ready sizes, in one bottom-up pass."""
    totals: dict[int, tuple[int, int, int]] = {}
    size_totals: dict[int, tuple[int, int]] = {}
    get_record = result.records.get
    get_children = result.children.get
    # Count files while discovering folders, rather than reading every child twice.
    stack = [(result.root_frn, None)]
    active = set()
    while stack:
        frn, pending = stack.pop()
        if pending is None:
            if frn in totals or frn in active:
                continue
            active.add(frn)
            child_folders = []
            files = logical = allocated = 0
            if include_sizes:
                own = result.records[frn]
                allocated = own.allocated_size if own.size_ready else 0
            for child_frn in get_children(frn, ()):
                child = get_record(child_frn)
                if child is None:
                    continue
                if child.attributes & FILE_ATTRIBUTE_DIRECTORY:
                    child_folders.append(child_frn)
                else:
                    files += 1
                    if include_sizes:
                        logical += child.logical_size
                        allocated += child.allocated_size
            stack.append((frn, (child_folders, files, logical, allocated)))
            for child_frn in child_folders:
                stack.append((child_frn, None))
            continue
        child_folders, files, logical, allocated = pending
        active.remove(frn)
        folders = 0
        for child_frn in child_folders:
            child_items, child_files, nested_folders = totals.get(child_frn, (0, 0, 0))
            files += child_files
            folders += nested_folders + 1
            if include_sizes:
                child_logical, child_allocated = size_totals.get(child_frn, (0, 0))
                logical += child_logical
                allocated += child_allocated
        totals[frn] = (files + folders, files, folders)
        if include_sizes:
            size_totals[frn] = (logical, allocated)
    result.stats = totals
    if include_sizes:
        result.directory_sizes = size_totals
        result.sizes_complete = True
    return totals


def calculate_directory_sizes(result: ScanResult) -> dict[int, tuple[int, int]]:
    """Aggregate file sizes bottom-up without revisiting the filesystem."""
    totals: dict[int, tuple[int, int]] = {}
    for frn in _directory_postorder(result):
        logical = 0
        own = result.records[frn]
        allocated = own.allocated_size if own.size_ready else 0  # Directory index/metadata streams.
        for child_frn in result.children.get(frn, ()):
            child = result.records.get(child_frn)
            if child is None:
                continue
            if child.is_directory:
                child_logical, child_allocated = totals.get(child_frn, (0, 0))
                logical += child_logical
                allocated += child_allocated
            else:
                child_logical = child.logical_size
                logical += child_logical
                allocated += child.allocated_size
        totals[frn] = (logical, allocated)
    result.directory_sizes = totals
    result.sizes_complete = True
    return totals
