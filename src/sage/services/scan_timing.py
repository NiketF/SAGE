"""Opt-in local timing reports. Diagnostic I/O must never affect app success."""
import json
import os
from pathlib import Path
import tempfile
import time


def begin_scan_timings(progress, drive):
    if os.environ.get('SAGE_SCAN_TIMINGS') != '1':
        return None
    report = {'backend': 'python_mft', 'status': 'running', 'drive': drive,
              'started_unix_seconds': time.time(), 'mft_read_seconds': 0.0,
              'record_parsing_seconds': 0.0}
    if hasattr(progress, '__dict__'):
        progress.scan_timings = report
    write_scan_timings(report)
    return report


def write_scan_timings(report):
    if report is None or os.environ.get('SAGE_SCAN_TIMINGS') != '1':
        return
    _write_report(report, 'scan-timing-last.json')


def write_ai_timings(report):
    if report is not None and os.environ.get('SAGE_AI_TIMINGS') == '1':
        _write_report(report, 'ai-timing-last.json')


def _write_report(report, filename):
    temporary = None
    try:
        folder = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'SAGE'
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=folder,
                prefix=filename+'-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, indent=2)
        temporary.replace(folder / filename)
    except (OSError, ValueError, TypeError):
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
