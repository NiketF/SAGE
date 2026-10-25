//! Python bindings for SAGE's native `$MFT` scanner.
//!
//! Results are returned as columns (one list per field) so Python builds its
//! `FileRecord` objects in a single pass without re-parsing anything.

use std::collections::HashMap;
use std::io;

use pyo3::exceptions::{PyInterruptedError, PyOSError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

mod mft;
#[cfg(windows)]
mod volume;

/// Bumped whenever the Python-facing result layout changes.
const ABI_VERSION: u32 = 1;

pub enum ScanError {
    Io(io::Error),
    Message(String),
    Cancelled,
    Python(PyErr),
}

impl ScanError {
    pub fn message(text: &str) -> Self {
        ScanError::Message(text.to_owned())
    }
}

impl From<io::Error> for ScanError {
    fn from(error: io::Error) -> Self {
        ScanError::Io(error)
    }
}

impl From<ScanError> for PyErr {
    fn from(error: ScanError) -> Self {
        match error {
            ScanError::Io(error) => error.into(),
            ScanError::Message(text) => PyOSError::new_err(text),
            ScanError::Cancelled => PyInterruptedError::new_err("MFT scan cancelled."),
            ScanError::Python(error) => error,
        }
    }
}

fn to_python(py: Python<'_>, finished: mft::Finished) -> PyResult<PyObject> {
    let mft::Finished { entries, aliases, skipped } = finished;
    let result = PyDict::new_bound(py);
    result.set_item("frn", PyList::new_bound(py, entries.iter().map(|e| e.frn)))?;
    result.set_item("parent", PyList::new_bound(py, entries.iter().map(|e| e.parent)))?;
    result.set_item("name", PyList::new_bound(py, entries.iter().map(|e| e.name.as_str())))?;
    // A few hundred distinct attribute values cover millions of records; share one int each.
    let mut shared: HashMap<u32, PyObject> = HashMap::new();
    let attributes: Vec<PyObject> = entries
        .iter()
        .map(|e| shared.entry(e.attributes).or_insert_with(|| e.attributes.into_py(py)).clone_ref(py))
        .collect();
    result.set_item("attributes", PyList::new_bound(py, attributes))?;
    result.set_item("logical", PyList::new_bound(py, entries.iter().map(|e| e.logical)))?;
    result.set_item("allocated", PyList::new_bound(py, entries.iter().map(|e| e.allocated)))?;
    result.set_item("modified", PyList::new_bound(py, entries.iter().map(|e| e.modified)))?;
    result.set_item("link_count", PyList::new_bound(py, entries.iter().map(|e| e.link_count)))?;
    drop(entries);
    result.set_item("alias_id", PyList::new_bound(py, aliases.iter().map(|a| a.id)))?;
    result.set_item("alias_parent", PyList::new_bound(py, aliases.iter().map(|a| a.parent)))?;
    result.set_item("alias_name", PyList::new_bound(py, aliases.iter().map(|a| a.name.as_str())))?;
    result.set_item("alias_target", PyList::new_bound(py, aliases.iter().map(|a| a.target)))?;
    result.set_item("skipped", skipped)?;
    Ok(result.into_any().unbind())
}

/// Read and parse a volume's `$MFT`.
///
/// `progress(records, fraction)` is called periodically and `cancelled()` is
/// polled between read chunks; both run with the GIL held.
#[pyfunction]
#[pyo3(signature = (drive, buffer_size, progress_interval, progress=None, cancelled=None))]
fn scan_volume(
    py: Python<'_>,
    drive: &str,
    buffer_size: usize,
    progress_interval: usize,
    progress: Option<PyObject>,
    cancelled: Option<PyObject>,
) -> PyResult<PyObject> {
    #[cfg(windows)]
    {
        let drive = drive.to_owned();
        let finished = py.allow_threads(|| {
            volume::scan(&drive, buffer_size.max(64 * 1024), progress_interval.max(1), |report| {
                Python::with_gil(|py| {
                    if let Some(cancelled) = &cancelled {
                        let stop = cancelled
                            .call0(py)
                            .and_then(|value| value.bind(py).is_truthy())
                            .map_err(ScanError::Python)?;
                        if stop {
                            return Err(ScanError::Cancelled);
                        }
                    }
                    if let (Some(progress), Some((count, fraction))) = (&progress, report) {
                        progress.call1(py, (count, fraction)).map_err(ScanError::Python)?;
                    }
                    Ok(())
                })
            })
        })?;
        to_python(py, finished)
    }
    #[cfg(not(windows))]
    {
        let _ = (py, drive, buffer_size, progress_interval, progress, cancelled);
        Err(PyOSError::new_err("Native MFT enumeration requires Windows."))
    }
}

/// Parse an in-memory `$MFT` image whose first record is MFT entry 0.
#[pyfunction]
fn parse_mft_image(py: Python<'_>, data: &[u8], record_size: usize) -> PyResult<PyObject> {
    if record_size < 0x30 {
        return Err(PyValueError::new_err("record_size is too small for an NTFS FILE record."));
    }
    let data = data.to_vec();
    let finished = py.allow_threads(|| {
        let mut accumulator = mft::Accumulator::new(record_size);
        accumulator.feed(data);
        accumulator.finish()
    });
    to_python(py, finished)
}

#[pymodule]
fn sage_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("ABI_VERSION", ABI_VERSION)?;
    m.add_function(wrap_pyfunction!(scan_volume, m)?)?;
    m.add_function(wrap_pyfunction!(parse_mft_image, m)?)?;
    Ok(())
}
