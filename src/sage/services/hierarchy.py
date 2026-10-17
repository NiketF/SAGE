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
    dir_by_number: dict[int, int] = {
        record.frn & NTFS_RECORD_NUMBER_MASK: record.frn
        for record in records.values()
        if record.is_directory
    }

    # Resolve parent references even when sequence numbers differ or are omitted
    for record in records.values():
        if record.frn == root_frn:
            continue
        parent = records.get(record.parent_frn)
        if parent is None:
            p_num = record.parent_frn & NTFS_RECORD_NUMBER_MASK
            if p_num in dir_by_number:
                record.parent_frn = dir_by_number[p_num]
                parent = records.get(record.parent_frn)
        if parent is None or not parent.is_directory or record.parent_frn == record.frn:
            record.parent_frn = root_frn

    # ponytail: directory cycle breaking attaches the loop's first visited node to root_frn;
    # full path repair from secondary evidence is an upgrade path if broken NTFS trees need original names preserved.
    # Detect and break directory cycles in linear time to ensure complete root reachability
    reachable: set[int] = {root_frn}
    for record in records.values():
        if not record.is_directory or record.frn == root_frn:
            continue
        curr = record.frn
        path: list[int] = []
        seen: set[int] = set()
        while curr != root_frn:
            if curr in reachable:
                break
            if curr in seen:
                records[curr].parent_frn = root_frn
                break
            seen.add(curr)
            path.append(curr)
            parent = records.get(records[curr].parent_frn)
            if parent is None or not parent.is_directory or parent.frn == curr:
                records[curr].parent_frn = root_frn
                break
            curr = parent.frn
        reachable.update(path)

    children: defaultdict[int, list[int]] = defaultdict(list)
    dir_children: defaultdict[int, list[int]] = defaultdict(list)
    all_sizes_ready = True
    file_count = 0

    for record in records.values():
        if record.frn == root_frn:
            continue
        children[record.parent_frn].append(record.frn)
        if record.is_directory:
            dir_children[record.parent_frn].append(record.frn)
        else:
            file_count += 1
            if not record.size_ready:
                all_sizes_ready = False

    result = ScanResult(
        volume=volume,
        records=records,
        children=dict(children),
        root_frn=root_frn,
        skipped_records=skipped_records,
    )
    order = _directory_postorder(result, dir_children=dir_children)
    if all_sizes_ready:
        _calculate_combined_totals(result, order)
        result.sized_files = file_count
    else:
        calculate_stats(result, order=order)
    return result


def _directory_postorder(
    result: ScanResult,
    dir_children: dict[int, list[int]] | None = None,
) -> list[int]:
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
        if dir_children is not None:
            for child_frn in dir_children.get(frn, ()):
                stack.append((child_frn, False))
        else:
            for child_frn in result.children.get(frn, ()):
                child = result.records.get(child_frn)
                if child and child.is_directory:
                    stack.append((child_frn, False))
    return order


def _calculate_combined_totals(result: ScanResult, order: list[int]) -> None:
    stat_totals: dict[int, tuple[int, int, int]] = {}
    size_totals: dict[int, tuple[int, int]] = {}
    records = result.records
    children_map = result.children

    for frn in order:
        files = 0
        folders = 0
        logical = 0
        allocated = 0
        for child_frn in children_map.get(frn, ()):
            child = records.get(child_frn)
            if child is None:
                continue
            if child.is_directory:
                child_items, child_files, child_folders = stat_totals.get(child_frn, (0, 0, 0))
                files += child_files
                folders += child_folders + 1
                child_logical, child_allocated = size_totals.get(child_frn, (0, 0))
                logical += child_logical
                allocated += child_allocated
            else:
                files += 1
                logical += child.logical_size
                allocated += child.allocated_size
        stat_totals[frn] = (files + folders, files, folders)
        size_totals[frn] = (logical, allocated)

    result.stats = stat_totals
    result.directory_sizes = size_totals
    result.sizes_complete = True


def calculate_stats(
    result: ScanResult, order: list[int] | None = None
) -> dict[int, tuple[int, int, int]]:
    """Map each directory to recursive (items, files, folders) counts."""
    totals: dict[int, tuple[int, int, int]] = {}
    if order is None:
        order = _directory_postorder(result)
    for frn in order:
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


def calculate_directory_sizes(
    result: ScanResult, order: list[int] | None = None
) -> dict[int, tuple[int, int]]:
    """Aggregate file sizes bottom-up without revisiting the filesystem."""
    totals: dict[int, tuple[int, int]] = {}
    if order is None:
        order = _directory_postorder(result)
    for frn in order:
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
