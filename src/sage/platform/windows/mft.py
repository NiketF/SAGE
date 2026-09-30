"""Enumerate NTFS file records through FSCTL_ENUM_USN_DATA.

This is a native MFT-backed enumeration. It intentionally does not fall back
to os.walk(): a failed native scan should remain visible and diagnosable.
"""

from __future__ import annotations

import ctypes
import os
import struct
from collections.abc import Callable, Iterator
from ctypes import wintypes

from ...config import MFT_BUFFER_SIZE, MFT_PROGRESS_INTERVAL
from ...models import FileRecord

FSCTL_ENUM_USN_DATA = 0x000900B3
FSCTL_GET_NTFS_VOLUME_DATA = 0x00090064
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
ERROR_HANDLE_EOF = 38
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
USN_RECORD_V2_HEADER_SIZE = 60
NTFS_RECORD_NUMBER_MASK = (1 << 48) - 1


class MFT_ENUM_DATA_V0(ctypes.Structure):
    _fields_ = [
        ("StartFileReferenceNumber", ctypes.c_ulonglong),
        ("LowUsn", ctypes.c_longlong),
        ("HighUsn", ctypes.c_longlong),
    ]


class NTFS_VOLUME_DATA_BUFFER(ctypes.Structure):
    _fields_ = [
        ("VolumeSerialNumber", ctypes.c_longlong),
        ("NumberSectors", ctypes.c_longlong),
        ("TotalClusters", ctypes.c_longlong),
        ("FreeClusters", ctypes.c_longlong),
        ("TotalReserved", ctypes.c_longlong),
        ("BytesPerSector", wintypes.DWORD),
        ("BytesPerCluster", wintypes.DWORD),
        ("BytesPerFileRecordSegment", wintypes.DWORD),
        ("ClustersPerFileRecordSegment", wintypes.DWORD),
        ("MftValidDataLength", ctypes.c_longlong),
        ("MftStartLcn", ctypes.c_longlong),
        ("Mft2StartLcn", ctypes.c_longlong),
        ("MftZoneStart", ctypes.c_longlong),
        ("MftZoneEnd", ctypes.c_longlong),
    ]


def _mft_record_capacity(kernel32, handle: int) -> int | None:
    """Estimate scan progress from the allocated MFT record range."""
    volume = NTFS_VOLUME_DATA_BUFFER()
    returned = wintypes.DWORD()
    ok = kernel32.DeviceIoControl(
        handle,
        FSCTL_GET_NTFS_VOLUME_DATA,
        None,
        0,
        ctypes.byref(volume),
        ctypes.sizeof(volume),
        ctypes.byref(returned),
        None,
    )
    if not ok or not volume.BytesPerFileRecordSegment or volume.MftValidDataLength <= 0:
        return None
    return (volume.MftValidDataLength + volume.BytesPerFileRecordSegment - 1) // volume.BytesPerFileRecordSegment


def parse_usn_records(data: bytes) -> Iterator[FileRecord]:
    """Parse the records after the leading next-FRN cursor."""
    offset = 8
    length = len(data)
    while offset + USN_RECORD_V2_HEADER_SIZE <= length:
        record_length = struct.unpack_from("<I", data, offset)[0]
        if record_length < USN_RECORD_V2_HEADER_SIZE or offset + record_length > length:
            break

        major_version = struct.unpack_from("<H", data, offset + 4)[0]
        if major_version == 2:
            frn = struct.unpack_from("<Q", data, offset + 8)[0]
            parent_frn = struct.unpack_from("<Q", data, offset + 16)[0]
            attributes = struct.unpack_from("<I", data, offset + 52)[0]
            name_length = struct.unpack_from("<H", data, offset + 56)[0]
            name_offset = struct.unpack_from("<H", data, offset + 58)[0]
            name_start = offset + name_offset
            name_end = name_start + name_length
            if name_end <= offset + record_length:
                name = data[name_start:name_end].decode("utf-16-le", errors="replace")
                yield FileRecord(frn, parent_frn, name, attributes)

        offset += record_length


def enumerate_mft(
    drive: str,
    progress: Callable[[int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[dict[int, FileRecord], int]:
    """Return active NTFS records keyed by file reference number."""
    if os.name != "nt":
        raise RuntimeError("Native MFT enumeration requires Windows.")

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
    kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.DeviceIoControl.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    volume_path = "\\\\.\\" + drive.rstrip("\\/")
    handle = kernel32.CreateFileW(
        volume_path,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())

    records: dict[int, FileRecord] = {}
    skipped = 0
    cursor = 0
    last_reported = 0
    output = ctypes.create_string_buffer(MFT_BUFFER_SIZE)
    progress_details = getattr(progress, "on_details", None)

    try:
        capacity = _mft_record_capacity(kernel32, handle)
        if progress:
            progress(0)
        if progress_details:
            progress_details(0, 0.0 if capacity else None)
        while True:
            if cancelled and cancelled():
                raise InterruptedError("MFT scan cancelled.")

            query = MFT_ENUM_DATA_V0(cursor, 0, 0x7FFFFFFFFFFFFFFF)
            returned = wintypes.DWORD()
            ok = kernel32.DeviceIoControl(
                handle,
                FSCTL_ENUM_USN_DATA,
                ctypes.byref(query),
                ctypes.sizeof(query),
                output,
                len(output),
                ctypes.byref(returned),
                None,
            )
            if not ok:
                error = ctypes.get_last_error()
                if error == ERROR_HANDLE_EOF:
                    break
                raise ctypes.WinError(error)
            if returned.value < 8:
                break

            chunk = ctypes.string_at(output, returned.value)
            next_cursor = struct.unpack_from("<Q", chunk, 0)[0]
            for record in parse_usn_records(chunk):
                if record.name:
                    records[record.frn] = record
                else:
                    skipped += 1

            if (progress or progress_details) and len(records) - last_reported >= MFT_PROGRESS_INTERVAL:
                fraction = (
                    min(0.99, (next_cursor & NTFS_RECORD_NUMBER_MASK) / capacity)
                    if capacity else None
                )
                if progress:
                    progress(len(records))
                if progress_details:
                    progress_details(len(records), fraction)
                last_reported = len(records)
            if next_cursor <= cursor:
                break
            cursor = next_cursor
    finally:
        kernel32.CloseHandle(handle)

    if progress:
        progress(len(records))
    if progress_details:
        progress_details(len(records), 1.0)
    return records, skipped
