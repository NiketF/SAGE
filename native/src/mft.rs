//! NTFS FILE record parsing and record assembly.
//!
//! Behaviour mirrors `sage.platform.windows.mft` exactly; the Python module is
//! the reference implementation and `tests/test_native_mft.py` checks parity.

use std::collections::HashMap;

use rayon::prelude::*;

const ATTR_STANDARD_INFORMATION: u32 = 0x10;
const ATTR_ATTRIBUTE_LIST: u32 = 0x20;
const ATTR_FILE_NAME: u32 = 0x30;
pub const ATTR_DATA: u32 = 0x80;
const ATTR_REPARSE_POINT: u32 = 0xC0;
const ATTR_END: u32 = 0xFFFF_FFFF;
const ATTR_FLAG_COMPRESSED: u16 = 0x0001;
const ATTR_FLAG_SPARSE: u16 = 0x8000;
const FILE_ATTRIBUTE_DIRECTORY: u32 = 0x10;
const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
const REPARSE_TAG_NAME_SURROGATE: u32 = 0x2000_0000;
const FILETIME_UNIX_EPOCH: u64 = 116_444_736_000_000_000;
const MFT_IN_USE: u16 = 0x0001;
const MFT_IS_DIRECTORY: u16 = 0x0002;
const NTFS_RECORD_NUMBER_MASK: u64 = (1 << 48) - 1;
const MFT_USA_STRIDE: usize = 512;

#[inline]
pub fn u16_at(b: &[u8], o: usize) -> Option<u16> {
    b.get(o..o.checked_add(2)?).map(|s| u16::from_le_bytes([s[0], s[1]]))
}

#[inline]
pub fn u32_at(b: &[u8], o: usize) -> Option<u32> {
    b.get(o..o.checked_add(4)?).map(|s| u32::from_le_bytes(s.try_into().unwrap()))
}

#[inline]
pub fn u64_at(b: &[u8], o: usize) -> Option<u64> {
    b.get(o..o.checked_add(8)?).map(|s| u64::from_le_bytes(s.try_into().unwrap()))
}

#[inline]
fn i64_at(b: &[u8], o: usize) -> Option<i64> {
    u64_at(b, o).map(|v| v as i64)
}

/// Restore the Update Sequence Array so sector-end bytes are real attribute data.
pub fn apply_fixup(record: &mut [u8]) -> bool {
    let end = record.len();
    if end < 8 {
        return false;
    }
    let usa_offset = u16_at(record, 4).unwrap() as usize;
    let usa_count = u16_at(record, 6).unwrap() as usize;
    if usa_count < 2 || usa_offset + usa_count * 2 > end {
        return false;
    }
    let usn = [record[usa_offset], record[usa_offset + 1]];
    for i in 1..usa_count {
        let sector_end = i * MFT_USA_STRIDE;
        if sector_end > end || sector_end < 2 {
            return false;
        }
        if record[sector_end - 2..sector_end] != usn {
            return false;
        }
        let src = usa_offset + i * 2;
        record[sector_end - 2] = record[src];
        record[sector_end - 1] = record[src + 1];
    }
    true
}

/// Decode NTFS mapping pairs into (lcn, cluster_count); lcn is -1 if sparse.
pub fn parse_data_runs(data: &[u8]) -> Vec<(i64, u64)> {
    let mut runs = Vec::new();
    let mut offset = 0usize;
    let mut lcn: i64 = 0;
    while offset < data.len() {
        let header = data[offset];
        offset += 1;
        if header == 0 {
            break;
        }
        let length_size = (header & 0x0F) as usize;
        let offset_size = (header >> 4) as usize;
        if length_size == 0 || length_size > 8 || offset_size > 8 || offset + length_size + offset_size > data.len() {
            break;
        }
        let cluster_count = le_uint(&data[offset..offset + length_size]);
        offset += length_size;
        if cluster_count == 0 {
            break;
        }
        if offset_size == 0 {
            runs.push((-1, cluster_count));
            continue;
        }
        let raw = le_uint(&data[offset..offset + offset_size]);
        let bits = offset_size * 8;
        let rel = if bits < 64 && raw >= 1 << (bits - 1) {
            raw as i64 - (1i64 << bits)
        } else {
            raw as i64
        };
        lcn += rel;
        if lcn < 0 {
            break;
        }
        runs.push((lcn, cluster_count));
        offset += offset_size;
    }
    runs
}

fn le_uint(bytes: &[u8]) -> u64 {
    bytes.iter().rev().fold(0u64, |acc, &b| (acc << 8) | b as u64)
}

pub type StreamKey = (u32, Vec<u8>);

/// Insertion-ordered stream sizes keyed by (attribute type, stream name).
#[derive(Default, Clone, Debug)]
pub struct Streams(Vec<(StreamKey, (u64, u64))>);

impl Streams {
    fn insert(&mut self, key: StreamKey, value: (u64, u64)) {
        match self.0.iter_mut().find(|(k, _)| *k == key) {
            Some(entry) => entry.1 = value,
            None => self.0.push((key, value)),
        }
    }

    fn update(&mut self, other: &Streams) {
        for (key, value) in &other.0 {
            self.insert(key.clone(), *value);
        }
    }

    fn unnamed_data_logical(&self) -> u64 {
        self.0
            .iter()
            .find(|(k, _)| k.0 == ATTR_DATA && k.1.is_empty())
            .map_or(0, |(_, v)| v.0)
    }

    fn allocated_total(&self) -> u64 {
        self.0.iter().fold(0u64, |acc, (_, v)| acc.saturating_add(v.1))
    }
}

#[derive(Debug)]
pub struct ParsedRecord {
    pub base_ref: u64,
    pub sequence: u16,
    pub is_directory: bool,
    pub name: String,
    pub parent: u64,
    pub logical: u64,
    pub allocated: u64,
    pub modified: u64,
    pub attributes: u32,
    pub names: Vec<(u64, String)>,
    pub streams: Streams,
    pub attribute_list: bool,
}

fn file_name_attr(record: &[u8], offset: usize) -> Option<(String, u64, u8)> {
    let value_len = u32_at(record, offset + 0x10)? as usize;
    let value_off = u16_at(record, offset + 0x14)? as usize;
    let value_start = offset + value_off;
    if value_off < 0x18 || value_len < 0x42 || value_start + value_len > record.len() {
        return None;
    }
    let parent = u64_at(record, value_start)?;
    let chars = record[value_start + 0x40] as usize;
    let namespace = record[value_start + 0x41];
    let name_end = value_start + 0x42 + chars * 2;
    if name_end > value_start + value_len {
        return None;
    }
    let units: Vec<u16> = record[value_start + 0x42..name_end]
        .chunks_exact(2)
        .map(|c| u16::from_le_bytes([c[0], c[1]]))
        .collect();
    let name = String::from_utf16_lossy(&units);
    if name.is_empty() {
        return None;
    }
    let rank = match namespace {
        3 => 3,
        1 => 2,
        0 => 1,
        _ => 0,
    };
    Some((name, parent, rank))
}

fn data_sizes(record: &[u8], offset: usize, attr_len: usize, non_resident: bool) -> Option<(u64, u64)> {
    let attr_end = record.len().min(offset + attr_len);
    if non_resident {
        if offset + 0x40 > attr_end {
            return None;
        }
        if i64_at(record, offset + 0x10)? != 0 {
            return None;
        }
        let flags = u16_at(record, offset + 0x0C)?;
        let allocated = if flags & (ATTR_FLAG_COMPRESSED | ATTR_FLAG_SPARSE) != 0 {
            if offset + 0x48 > attr_end {
                return None;
            }
            i64_at(record, offset + 0x40)?.max(0)
        } else {
            i64_at(record, offset + 0x28)?.max(0)
        };
        let logical = i64_at(record, offset + 0x30)?.max(0);
        return Some((logical as u64, allocated as u64));
    }
    if offset + 0x18 > attr_end {
        return None;
    }
    Some((u32_at(record, offset + 0x10)? as u64, 0))
}

/// Parse one fixed-up FILE record; `None` matches the Python parser returning None.
pub fn parse_record(record: &[u8]) -> Option<ParsedRecord> {
    if record.len() < 0x30 || &record[0..4] != b"FILE" {
        return None;
    }
    let flags = u16_at(record, 0x16)?;
    if flags & MFT_IN_USE == 0 {
        return None;
    }
    let sequence = u16_at(record, 0x10)?;
    let first_attr = u16_at(record, 0x14)? as usize;
    let used = u32_at(record, 0x18)? as usize;
    let base_ref = u64_at(record, 0x20)?;
    let is_directory = flags & MFT_IS_DIRECTORY != 0;
    let limit = used.min(record.len());

    let mut name = String::new();
    let mut parent = 0u64;
    let mut name_rank: i32 = -1;
    let mut modified = 0u64;
    let mut attributes = 0u32;
    let mut reparse_tag: Option<u32> = None;
    let mut names: Vec<(u64, String)> = Vec::new();
    let mut streams = Streams::default();
    let mut attribute_list = false;

    let mut offset = first_attr;
    while offset + 16 <= limit {
        let attr_type = u32_at(record, offset)?;
        if attr_type == ATTR_END {
            break;
        }
        let attr_len = u32_at(record, offset + 4)? as usize;
        if attr_len < 16 || offset + attr_len > limit {
            return None;
        }
        let non_resident = record[offset + 8] != 0;
        let name_len = record[offset + 9] as usize;
        if (attr_type == ATTR_STANDARD_INFORMATION || attr_type == ATTR_REPARSE_POINT) && !non_resident {
            let value_len = u32_at(record, offset + 0x10)? as usize;
            let value_start = offset + u16_at(record, offset + 0x14)? as usize;
            if attr_type == ATTR_STANDARD_INFORMATION && value_len >= 0x24 && value_start + 0x24 <= limit {
                let filetime = u64_at(record, value_start + 8)?;
                modified = filetime.saturating_sub(FILETIME_UNIX_EPOCH) / 10_000_000;
                attributes = u32_at(record, value_start + 0x20)?;
            } else if attr_type == ATTR_REPARSE_POINT && value_len >= 4 && value_start + 4 <= limit {
                reparse_tag = Some(u32_at(record, value_start)?);
            }
        } else if attr_type == ATTR_ATTRIBUTE_LIST {
            attribute_list = true;
            if non_resident {
                if let Some(sizes) = data_sizes(record, offset, attr_len, true) {
                    streams.insert((attr_type, Vec::new()), sizes);
                }
            }
        } else if attr_type == ATTR_FILE_NAME && !non_resident {
            if let Some((candidate, parent_ref, rank)) = file_name_attr(record, offset) {
                if rank > 0 && !names.iter().any(|(p, n)| *p == parent_ref && *n == candidate) {
                    names.push((parent_ref, candidate.clone()));
                }
                if rank as i32 > name_rank {
                    name = candidate;
                    parent = parent_ref;
                    name_rank = rank as i32;
                }
            }
        } else if attr_type == ATTR_DATA || non_resident {
            let name_off = u16_at(record, offset + 0x0A)? as usize;
            if name_len != 0 && (name_off < 16 || name_off + name_len * 2 > attr_len) {
                return None;
            }
            let stream_name = if name_len != 0 {
                record[offset + name_off..offset + name_off + name_len * 2].to_vec()
            } else {
                Vec::new()
            };
            if let Some(sizes) = data_sizes(record, offset, attr_len, non_resident) {
                streams.insert((attr_type, stream_name), sizes);
            }
        }
        offset += attr_len;
    }
    if let Some(tag) = reparse_tag {
        if tag & REPARSE_TAG_NAME_SURROGATE == 0 {
            attributes &= !FILE_ATTRIBUTE_REPARSE_POINT;
        }
    }
    let logical = streams.unnamed_data_logical();
    let allocated = streams.allocated_total();
    Some(ParsedRecord {
        base_ref,
        sequence,
        is_directory,
        name,
        parent,
        logical,
        allocated,
        modified,
        attributes,
        names,
        streams,
        attribute_list,
    })
}

enum Outcome {
    Ignore,
    Skipped,
    Parsed { frn: u64, link_count: u16, record: ParsedRecord },
}

fn process(record: &mut [u8], index: u64) -> Outcome {
    if record.len() < 0x18 || &record[0..4] != b"FILE" {
        return Outcome::Ignore;
    }
    if u16_at(record, 0x16).unwrap() & MFT_IN_USE == 0 {
        return Outcome::Ignore;
    }
    if !apply_fixup(record) {
        return Outcome::Skipped;
    }
    match parse_record(record) {
        None => Outcome::Skipped,
        Some(parsed) => Outcome::Parsed {
            frn: (index & NTFS_RECORD_NUMBER_MASK) | ((parsed.sequence as u64) << 48),
            link_count: u16_at(record, 0x12).unwrap_or(0),
            record: parsed,
        },
    }
}

pub struct Entry {
    pub frn: u64,
    pub parent: u64,
    pub name: String,
    pub attributes: u32,
    pub logical: u64,
    pub allocated: u64,
    pub modified: u64,
    pub link_count: u32,
}

impl Entry {
    fn is_directory(&self) -> bool {
        self.attributes & FILE_ATTRIBUTE_DIRECTORY != 0
    }
}

pub struct Alias {
    pub id: i64,
    pub parent: u64,
    pub name: String,
    pub target: u64,
}

#[derive(Default)]
struct Details {
    names: Vec<(u64, String)>,
    streams: Streams,
}

/// Insertion-ordered map, matching Python dict iteration order.
struct OrderedMap<V> {
    index: HashMap<u64, usize>,
    items: Vec<(u64, V)>,
}

impl<V> Default for OrderedMap<V> {
    fn default() -> Self {
        Self { index: HashMap::new(), items: Vec::new() }
    }
}

impl<V> OrderedMap<V> {
    fn entry_or_insert_with(&mut self, key: u64, make: impl FnOnce() -> V) -> &mut V {
        let pos = match self.index.get(&key) {
            Some(&pos) => pos,
            None => {
                self.items.push((key, make()));
                self.index.insert(key, self.items.len() - 1);
                self.items.len() - 1
            }
        };
        &mut self.items[pos].1
    }

    fn insert(&mut self, key: u64, value: V) {
        match self.index.get(&key) {
            Some(&pos) => self.items[pos].1 = value,
            None => {
                self.items.push((key, value));
                self.index.insert(key, self.items.len() - 1);
            }
        }
    }
}

pub struct Finished {
    pub entries: Vec<Entry>,
    pub aliases: Vec<Alias>,
    pub skipped: u64,
}

/// Accumulates whole FILE records in on-disk order, parsing each block in parallel.
pub struct Accumulator {
    record_size: usize,
    entries: Vec<Entry>,
    details: OrderedMap<Details>,
    extensions: OrderedMap<Details>,
    skipped: u64,
    record_index: u64,
    pending: Vec<u8>,
}

impl Accumulator {
    pub fn new(record_size: usize) -> Self {
        Self {
            record_size,
            entries: Vec::new(),
            details: OrderedMap::default(),
            extensions: OrderedMap::default(),
            skipped: 0,
            record_index: 0,
            pending: Vec::new(),
        }
    }

    pub fn record_count(&self) -> usize {
        self.entries.len()
    }

    pub fn feed(&mut self, mut data: Vec<u8>) {
        let rs = self.record_size;
        if self.pending.is_empty() {
            let usable = data.len() - data.len() % rs;
            let rest = data.split_off(usable);
            self.process_block(&mut data);
            self.pending = rest;
        } else {
            self.pending.extend_from_slice(&data);
            let usable = self.pending.len() - self.pending.len() % rs;
            let mut block: Vec<u8> = self.pending.drain(..usable).collect();
            self.process_block(&mut block);
        }
    }

    /// A sparse $MFT run: its records are unallocated and only advance the index.
    pub fn skip_records(&mut self, count: u64) {
        self.pending.clear();
        self.record_index += count;
    }

    fn process_block(&mut self, block: &mut [u8]) {
        if block.is_empty() {
            return;
        }
        let base = self.record_index;
        let outcomes: Vec<Outcome> = block
            .par_chunks_mut(self.record_size)
            .enumerate()
            .map(|(i, record)| process(record, base + i as u64))
            .collect();
        self.record_index += outcomes.len() as u64;
        for outcome in outcomes {
            self.apply(outcome);
        }
    }

    fn apply(&mut self, outcome: Outcome) {
        let (frn, link_count, record) = match outcome {
            Outcome::Ignore => return,
            Outcome::Skipped => {
                self.skipped += 1;
                return;
            }
            Outcome::Parsed { frn, link_count, record } => (frn, link_count, record),
        };
        if record.base_ref != 0 {
            let extra = self.extensions.entry_or_insert_with(record.base_ref, Details::default);
            extra.names.extend(record.names);
            extra.streams.update(&record.streams);
        } else if !record.name.is_empty() {
            if record.names.len() > 1 || record.attribute_list {
                self.details.insert(frn, Details { names: record.names, streams: record.streams });
            }
            let directory_flag = if record.is_directory { FILE_ATTRIBUTE_DIRECTORY } else { 0 };
            self.entries.push(Entry {
                frn,
                parent: record.parent,
                name: record.name,
                attributes: record.attributes | directory_flag,
                logical: record.logical,
                allocated: record.allocated,
                modified: record.modified,
                link_count: (link_count as u32).max(1),
            });
        } else {
            self.skipped += 1;
        }
    }

    /// Join extension attributes, then retain hard-link paths without duplicating allocation.
    pub fn finish(mut self) -> Finished {
        self.pending.clear();
        let mut aliases = Vec::new();
        if self.extensions.items.is_empty() && self.details.items.is_empty() {
            return Finished { entries: self.entries, aliases, skipped: self.skipped };
        }
        let index_of: HashMap<u64, usize> =
            self.entries.iter().enumerate().map(|(i, e)| (e.frn, i)).collect();

        let extensions = std::mem::take(&mut self.extensions.items);
        for (frn, extra) in extensions {
            let Some(&idx) = index_of.get(&frn) else {
                self.skipped += 1;
                continue;
            };
            let entry = &mut self.entries[idx];
            let details = self.details.entry_or_insert_with(frn, || Details {
                names: vec![(entry.parent, entry.name.clone())],
                streams: Streams(vec![((ATTR_DATA, Vec::new()), (entry.logical, entry.allocated))]),
            });
            details.names.extend(extra.names);
            details.streams.update(&extra.streams);
            entry.logical = details.streams.unnamed_data_logical();
            entry.allocated = details.streams.allocated_total();
        }

        let mut alias_id: i64 = -1;
        for (frn, details) in std::mem::take(&mut self.details.items) {
            let entry = &mut self.entries[index_of[&frn]];
            if entry.is_directory() {
                continue;
            }
            let mut unique: Vec<(u64, String)> = Vec::with_capacity(details.names.len());
            for pair in details.names {
                if !unique.contains(&pair) {
                    unique.push(pair);
                }
            }
            let current = (entry.parent, entry.name.clone());
            if !unique.is_empty() && !unique.contains(&current) {
                entry.parent = unique[0].0;
                entry.name = unique[0].1.clone();
            }
            let primary = (entry.parent, entry.name.clone());
            let union = unique.len() + usize::from(!unique.contains(&primary));
            entry.link_count = entry.link_count.max(union as u32);
            for (parent, name) in unique {
                if parent == primary.0 && name == primary.1 {
                    continue;
                }
                aliases.push(Alias { id: alias_id, parent, name, target: frn });
                alias_id -= 1;
            }
        }
        Finished { entries: self.entries, aliases, skipped: self.skipped }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn data_runs_include_sparse_and_relative_offsets() {
        let runs = parse_data_runs(&[0x11, 4, 8, 0x01, 8, 0x11, 2, 0xFD, 0]);
        assert_eq!(runs, vec![(8, 4), (-1, 8), (5, 2)]);
    }

    #[test]
    fn fixup_restores_sector_ends() {
        let mut record = vec![0u8; 1024];
        record[0..4].copy_from_slice(b"FILE");
        record[4..8].copy_from_slice(&[0x30, 0, 3, 0]);
        record[0x30..0x36].copy_from_slice(&[0xAB, 0xCD, 0x11, 0x22, 0x33, 0x44]);
        record[510..512].copy_from_slice(&[0xAB, 0xCD]);
        record[1022..1024].copy_from_slice(&[0xAB, 0xCD]);
        assert!(apply_fixup(&mut record));
        assert_eq!(&record[510..512], &[0x11, 0x22]);
        assert_eq!(&record[1022..1024], &[0x33, 0x44]);
    }
}
