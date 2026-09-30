"""Optional per-file size evidence collected after the fast MFT phase."""

from __future__ import annotations

import ctypes
import os
import struct
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from ctypes import wintypes
from functools import lru_cache

from ...config import SIZE_PROGRESS_INTERVAL, SIZE_WORKERS
from ...models import FileRecord, ScanResult

FILE_READ_ATTRIBUTES = 0x00000080
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_STANDARD_INFO_CLASS = 1
FILE_ID_BOTH_DIRECTORY_INFO_CLASS = 10
FILE_LIST_DIRECTORY = 0x00000001
ERROR_NO_MORE_FILES = 18
DIRECTORY_BUFFER_SIZE = 256 * 1024
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class FILE_STANDARD_INFO(ctypes.Structure):
    _fields_ = [
        ("AllocationSize", ctypes.c_longlong),
        ("EndOfFile", ctypes.c_longlong),
        ("NumberOfLinks", wintypes.DWORD),
        ("DeletePending", ctypes.c_ubyte),
        ("Directory", ctypes.c_ubyte),
    ]


class FILE_ID_BOTH_DIR_INFO_HEADER(ctypes.Structure):
    """Fixed part of FILE_ID_BOTH_DIR_INFO; FileName follows this header."""

    _fields_ = [
        ("NextEntryOffset", wintypes.DWORD),
        ("FileIndex", wintypes.DWORD),
        ("CreationTime", ctypes.c_longlong),
        ("LastAccessTime", ctypes.c_longlong),
        ("LastWriteTime", ctypes.c_longlong),
        ("ChangeTime", ctypes.c_longlong),
        ("EndOfFile", ctypes.c_longlong),
        ("AllocationSize", ctypes.c_longlong),
        ("FileAttributes", wintypes.DWORD),
        ("FileNameLength", wintypes.DWORD),
        ("EaSize", wintypes.DWORD),
        ("ShortNameLength", ctypes.c_ubyte),
        ("Reserved1", ctypes.c_ubyte),
        ("ShortName", ctypes.c_uint16 * 12),
        ("Reserved2", ctypes.c_uint16),
        ("FileId", ctypes.c_ulonglong),
    ]


_DIRECTORY_HEADER_SIZE = ctypes.sizeof(FILE_ID_BOTH_DIR_INFO_HEADER)


@lru_cache(maxsize=1)
def _kernel32():
    if os.name != "nt":
        raise RuntimeError("Native size collection requires Windows.")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def get_file_sizes(path: str) -> tuple[int, int]:
    kernel32 = _kernel32()
    if len(path) >= 248 and not path.startswith("\\\\?\\"):
        path = "\\\\?\\" + path

    handle = kernel32.CreateFileW(
        path,
        FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = FILE_STANDARD_INFO()
        if not kernel32.GetFileInformationByHandleEx(
            handle,
            FILE_STANDARD_INFO_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return max(0, info.EndOfFile), max(0, info.AllocationSize)
    finally:
        kernel32.CloseHandle(handle)


def _directory_entries(buffer: ctypes.Array) -> list[tuple[int, int, int]]:
    """Read file ID, logical size, and allocation from one Windows result buffer."""
    entries: list[tuple[int, int, int]] = []
    offset = 0
    while True:
        if offset + _DIRECTORY_HEADER_SIZE > len(buffer):
            raise ValueError("Truncated directory metadata buffer")
        next_offset = struct.unpack_from("<I", buffer, offset)[0]
        name_length = struct.unpack_from(
            "<I", buffer, offset + FILE_ID_BOTH_DIR_INFO_HEADER.FileNameLength.offset
        )[0]
        entry_end = offset + next_offset if next_offset else len(buffer)
        if (next_offset and next_offset < _DIRECTORY_HEADER_SIZE) or entry_end > len(buffer):
            raise ValueError("Invalid directory metadata entry length")
        if name_length % 2 or offset + _DIRECTORY_HEADER_SIZE + name_length > entry_end:
            raise ValueError("Invalid directory metadata filename length")
        logical = struct.unpack_from(
            "<q", buffer, offset + FILE_ID_BOTH_DIR_INFO_HEADER.EndOfFile.offset
        )[0]
        allocated = struct.unpack_from(
            "<q", buffer, offset + FILE_ID_BOTH_DIR_INFO_HEADER.AllocationSize.offset
        )[0]
        file_id = struct.unpack_from(
            "<Q", buffer, offset + FILE_ID_BOTH_DIR_INFO_HEADER.FileId.offset
        )[0]
        entries.append((file_id, max(0, logical), max(0, allocated)))
        if not next_offset:
            return entries
        offset += next_offset


def get_directory_file_sizes(path: str) -> list[tuple[int, int, int]]:
    """Enumerate an entire directory with batched NTFS IDs and both size fields."""
    kernel32 = _kernel32()
    if len(path) >= 248 and not path.startswith("\\\\?\\"):
        path = "\\\\?\\" + path
    handle = kernel32.CreateFileW(
        path,
        FILE_LIST_DIRECTORY,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        output = ctypes.create_string_buffer(DIRECTORY_BUFFER_SIZE)
        entries: list[tuple[int, int, int]] = []
        while True:
            if not kernel32.GetFileInformationByHandleEx(
                handle,
                FILE_ID_BOTH_DIRECTORY_INFO_CLASS,
                output,
                len(output),
            ):
                error = ctypes.get_last_error()
                if error == ERROR_NO_MORE_FILES:
                    return entries
                raise ctypes.WinError(error)
            entries.extend(_directory_entries(output))
    finally:
        kernel32.CloseHandle(handle)


def collect_file_sizes(
    result: ScanResult,
    progress: Callable[[int, int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Collect bounded, concurrent size evidence and update folder lower bounds."""
    files = [record for record in result.records.values() if not record.is_directory]
    total = len(files)
    result.directory_sizes.clear()
    result.sized_files = 0
    result.size_errors = 0
    result.sizes_complete = False
    if progress:
        progress(0, total, 0)

    def read_one(record: FileRecord, path: str | None) -> tuple[int, int, str | None]:
        if path is None:
            return 0, 0, "Path could not be reconstructed"
        try:
            logical, allocated = get_file_sizes(path)
            return logical, allocated, None
        except OSError as exc:
            return 0, 0, str(exc)

    def apply_one(record: FileRecord, data: tuple[int, int, str | None]) -> None:
        logical, allocated, error = data
        record.logical_size = logical
        record.allocated_size = allocated
        record.size_error = error
        record.size_ready = True
        if error:
            result.size_errors += 1
        else:
            parent_frn = record.parent_frn
            seen: set[int] = set()
            while parent_frn in result.records and parent_frn not in seen:
                seen.add(parent_frn)
                old_logical, old_allocated = result.directory_sizes.get(parent_frn, (0, 0))
                result.directory_sizes[parent_frn] = (
                    old_logical + logical,
                    old_allocated + allocated,
                )
                if parent_frn == result.root_frn:
                    break
                parent_frn = result.records[parent_frn].parent_frn
        result.sized_files += 1
        if progress and (result.sized_files % SIZE_PROGRESS_INTERVAL == 0 or result.sized_files == total):
            progress(result.sized_files, total, result.size_errors)

    # One directory query returns size and allocation for many files. Match by
    # NTFS file ID instead of relying on names, which may have hard links.
    directories = [record for record in result.records.values() if record.is_directory]
    directories.sort(key=lambda record: record.frn != result.root_frn)
    directory_iterator = iter(directories)
    directory_pending: dict[Future[list[tuple[int, int, int]]], int] = {}
    with ThreadPoolExecutor(max_workers=SIZE_WORKERS, thread_name_prefix="sage-directory") as pool:
        def fill_directories() -> None:
            while len(directory_pending) < SIZE_WORKERS * 4:
                try:
                    directory = next(directory_iterator)
                except StopIteration:
                    return
                path = result.path_for(directory.frn)
                if path is not None:
                    directory_pending[pool.submit(get_directory_file_sizes, path)] = directory.frn

        fill_directories()
        while directory_pending:
            if cancelled and cancelled():
                for future in directory_pending:
                    future.cancel()
                raise InterruptedError("Size analysis cancelled.")
            done, _ = wait(directory_pending, timeout=0.2, return_when=FIRST_COMPLETED)
            for future in done:
                parent_frn = directory_pending.pop(future)
                try:
                    entries = future.result()
                except (OSError, ValueError):
                    continue  # Per-file fallback below handles inaccessible directories.
                for frn, logical, allocated in entries:
                    record = result.records.get(frn)
                    if record and not record.is_directory and not record.size_ready and record.parent_frn == parent_frn:
                        apply_one(record, (logical, allocated, None))
            fill_directories()

    # Files omitted by directory enumeration (or in inaccessible directories)
    # still receive the original exact Windows metadata query.
    iterator = (record for record in files if not record.size_ready)
    pending: dict[Future[tuple[int, int, str | None]], FileRecord] = {}
    with ThreadPoolExecutor(max_workers=SIZE_WORKERS, thread_name_prefix="sage-size") as pool:
        def fill() -> None:
            while len(pending) < SIZE_WORKERS * 4:
                try:
                    record = next(iterator)
                except StopIteration:
                    return
                pending[pool.submit(read_one, record, result.path_for(record.frn))] = record

        fill()
        while pending:
            if cancelled and cancelled():
                for future in pending:
                    future.cancel()
                raise InterruptedError("Size analysis cancelled.")
            done, _ = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
            for future in done:
                record = pending.pop(future)
                apply_one(record, future.result())
            fill()
