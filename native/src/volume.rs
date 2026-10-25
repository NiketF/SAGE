//! Raw NTFS volume access: geometry, `$MFT` mapping pairs, and a pipelined reader.

use std::ffi::c_void;
use std::fs::{File, OpenOptions};
use std::io;
use std::os::windows::fs::{FileExt, OpenOptionsExt};
use std::os::windows::io::AsRawHandle;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::sync_channel;
use std::thread;

use crate::mft::{apply_fixup, parse_data_runs, u16_at, u32_at, Accumulator, Finished, ATTR_DATA};
use crate::ScanError;

const FSCTL_GET_NTFS_VOLUME_DATA: u32 = 0x0009_0064;
const FILE_SHARE_ALL: u32 = 0x1 | 0x2 | 0x4;
const FILE_FLAG_SEQUENTIAL_SCAN: u32 = 0x0800_0000;
const ATTR_END: u32 = 0xFFFF_FFFF;
/// Read-ahead depth: chunks buffered between the disk thread and the parser.
const PIPELINE_DEPTH: usize = 4;

#[repr(C)]
#[derive(Default)]
#[allow(non_snake_case)]
struct NtfsVolumeData {
    VolumeSerialNumber: i64,
    NumberSectors: i64,
    TotalClusters: i64,
    FreeClusters: i64,
    TotalReserved: i64,
    BytesPerSector: u32,
    BytesPerCluster: u32,
    BytesPerFileRecordSegment: u32,
    ClustersPerFileRecordSegment: u32,
    MftValidDataLength: i64,
    MftStartLcn: i64,
    Mft2StartLcn: i64,
    MftZoneStart: i64,
    MftZoneEnd: i64,
}

#[link(name = "kernel32")]
extern "system" {
    fn DeviceIoControl(
        device: *mut c_void,
        code: u32,
        in_buffer: *const c_void,
        in_size: u32,
        out_buffer: *mut c_void,
        out_size: u32,
        returned: *mut u32,
        overlapped: *mut c_void,
    ) -> i32;
}

struct Geometry {
    sector: u64,
    cluster: u64,
    record_size: usize,
    valid: u64,
    mft_start_lcn: u64,
}

fn volume_data(volume: &File) -> io::Result<Geometry> {
    let mut data = NtfsVolumeData::default();
    let mut returned = 0u32;
    let ok = unsafe {
        DeviceIoControl(
            volume.as_raw_handle() as *mut c_void,
            FSCTL_GET_NTFS_VOLUME_DATA,
            std::ptr::null(),
            0,
            &mut data as *mut _ as *mut c_void,
            std::mem::size_of::<NtfsVolumeData>() as u32,
            &mut returned,
            std::ptr::null_mut(),
        )
    };
    if ok == 0 {
        return Err(io::Error::last_os_error());
    }
    if data.BytesPerSector == 0
        || data.BytesPerCluster == 0
        || data.BytesPerFileRecordSegment == 0
        || data.MftValidDataLength <= 0
        || data.MftStartLcn < 0
    {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "NTFS volume geometry is unreadable."));
    }
    Ok(Geometry {
        sector: data.BytesPerSector as u64,
        cluster: data.BytesPerCluster as u64,
        record_size: data.BytesPerFileRecordSegment as usize,
        valid: data.MftValidDataLength as u64,
        mft_start_lcn: data.MftStartLcn as u64,
    })
}

/// Sector-aligned positional read; may return fewer bytes at the end of the volume.
fn read_at(volume: &File, offset: u64, size: usize, sector: u64) -> io::Result<Vec<u8>> {
    if size == 0 {
        return Ok(Vec::new());
    }
    let aligned_off = offset - offset % sector;
    let aligned_end = (offset + size as u64).div_ceil(sector) * sector;
    let mut buf = vec![0u8; (aligned_end - aligned_off) as usize];
    let read = volume.seek_read(&mut buf, aligned_off)?;
    let start = (offset - aligned_off) as usize;
    let keep = size.min(read.saturating_sub(start));
    buf.truncate(start + keep);
    if start > 0 {
        buf.drain(..start);
    }
    Ok(buf)
}

fn mft_data_runs(mut record: Vec<u8>) -> Result<Vec<(i64, u64)>, ScanError> {
    // Mapping pairs come from $MFT record 0 only, matching the Python scanner.
    if !apply_fixup(&mut record) {
        return Err(ScanError::message("$MFT record 0 failed Update Sequence fixup."));
    }
    let mut offset = u16_at(&record, 0x14).unwrap_or(0) as usize;
    let used = (u32_at(&record, 0x18).unwrap_or(0) as usize).min(record.len());
    let mut runs = Vec::new();
    while offset + 16 <= used {
        let attr_type = u32_at(&record, offset).unwrap();
        if attr_type == ATTR_END {
            break;
        }
        let attr_len = u32_at(&record, offset + 4).unwrap() as usize;
        if attr_len < 16 || offset + attr_len > used {
            break;
        }
        if attr_type == ATTR_DATA && record[offset + 9] == 0 {
            if record[offset + 8] == 0 {
                return Err(ScanError::message("$MFT unnamed $DATA is resident."));
            }
            let pairs_off = u16_at(&record, offset + 0x20).unwrap_or(0) as usize;
            if offset + pairs_off <= offset + attr_len {
                runs = parse_data_runs(&record[offset + pairs_off..offset + attr_len]);
            }
            break;
        }
        offset += attr_len;
    }
    if runs.is_empty() {
        return Err(ScanError::message("Could not read $MFT data runs from record 0."));
    }
    Ok(runs)
}

enum Message {
    Data(Vec<u8>),
    Sparse { records: u64, bytes: u64 },
    Failed(io::Error),
}

/// Progress callback: `Some((records, fraction))` when a report is due, `None`
/// for a cancellation check only. Returning an error stops the scan.
pub fn scan(
    drive: &str,
    buffer_size: usize,
    progress_interval: usize,
    mut check: impl FnMut(Option<(usize, f64)>) -> Result<(), ScanError>,
) -> Result<Finished, ScanError> {
    let volume_path = format!(r"\\.\{}", drive.trim_end_matches(['\\', '/']));
    let volume = OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_ALL)
        .custom_flags(FILE_FLAG_SEQUENTIAL_SCAN)
        .open(&volume_path)?;
    let geometry = volume_data(&volume)?;
    let Geometry { sector, cluster, record_size, valid, mft_start_lcn } = geometry;

    let first = read_at(&volume, mft_start_lcn * cluster, record_size, sector)?;
    if first.len() < record_size {
        return Err(ScanError::message("Could not read $MFT record 0."));
    }
    let runs = mft_data_runs(first)?;
    let mapped: u64 = runs.iter().map(|&(_, count)| count).sum::<u64>().saturating_mul(cluster);
    if mapped < valid {
        return Err(ScanError::message("$MFT requires additional mapping records; refusing an incomplete scan."));
    }
    check(Some((0, 0.0)))?;

    let chunk_size = (buffer_size.max(record_size) / record_size * record_size).max(record_size) as u64;
    let stop = AtomicBool::new(false);
    let mut accumulator = Accumulator::new(record_size);
    let mut bytes_seen = 0u64;
    let mut outcome: Result<(), ScanError> = Ok(());

    thread::scope(|scope| {
        let (tx, rx) = sync_channel::<Message>(PIPELINE_DEPTH);
        let volume = &volume;
        let stop = &stop;
        scope.spawn(move || {
            let mut seen = 0u64;
            for (lcn, cluster_count) in runs {
                if stop.load(Ordering::Relaxed) {
                    return;
                }
                let take = (cluster_count * cluster).min(valid.saturating_sub(seen));
                if take == 0 {
                    break;
                }
                if lcn < 0 {
                    seen += take;
                    let records = take / record_size as u64;
                    if tx.send(Message::Sparse { records, bytes: take }).is_err() {
                        return;
                    }
                    continue;
                }
                let mut disk_offset = lcn as u64 * cluster;
                let mut remaining = take;
                while remaining > 0 {
                    if stop.load(Ordering::Relaxed) {
                        return;
                    }
                    let chunk = remaining.min(chunk_size) as usize;
                    match read_at(volume, disk_offset, chunk, sector) {
                        Ok(data) if data.is_empty() => break,
                        Ok(data) => {
                            let n = data.len() as u64;
                            disk_offset += n;
                            remaining -= n;
                            seen += n;
                            if tx.send(Message::Data(data)).is_err() {
                                return;
                            }
                        }
                        Err(error) => {
                            let _ = tx.send(Message::Failed(error));
                            return;
                        }
                    }
                }
            }
        });

        let mut last_reported = 0usize;
        for message in rx.iter() {
            match message {
                Message::Data(data) => {
                    bytes_seen += data.len() as u64;
                    accumulator.feed(data);
                    let count = accumulator.record_count();
                    let report = if count - last_reported >= progress_interval {
                        last_reported = count;
                        Some((count, (bytes_seen as f64 / valid as f64).min(0.99)))
                    } else {
                        None
                    };
                    if let Err(error) = check(report) {
                        outcome = Err(error);
                        break;
                    }
                }
                Message::Sparse { records, bytes } => {
                    accumulator.skip_records(records);
                    bytes_seen += bytes;
                }
                Message::Failed(error) => {
                    outcome = Err(error.into());
                    break;
                }
            }
        }
        stop.store(true, Ordering::Relaxed);
        drop(rx);
    });
    outcome?;
    if bytes_seen < valid {
        return Err(ScanError::message("$MFT read ended before its valid length; scan is incomplete."));
    }
    Ok(accumulator.finish())
}
