"""Changes since a retained scan; never rewrite its measurements or hierarchy."""
from __future__ import annotations

import ntpath
import os
import stat


def normalized(path):
    return ntpath.normcase(ntpath.normpath(path))


def within(path, parent):
    path, parent = normalized(path), normalized(parent)
    return path == parent or path.startswith(parent.rstrip('\\') + '\\')


def observe_paths(paths):
    """Check selected targets only. Permission errors are unknown, never absence."""
    observed = {}
    for path in paths:
        try:
            info = os.lstat(path)
            observed[path] = (info.st_dev, info.st_ino, stat.S_ISDIR(info.st_mode))
        except (FileNotFoundError, NotADirectoryError):
            observed[path] = False
        except OSError:
            observed[path] = None
    return observed


class ScanChanges:
    def __init__(self):
        self.removed = {}
        self.changed = set()
        self.files = 0
        self.folders = 0
        self.stale = False

    def record(self, before, after, *, deletion=True):
        self.stale = True
        for path, previous in before.items():
            current = after.get(path)
            if previous and (current is False or current and current[:2] != previous[:2]):
                key = normalized(path)
                if key not in self.removed:
                    self.removed[key] = path
                    if deletion:
                        if previous[2]: self.folders += 1
                        else: self.files += 1
            else:
                self.changed.add(path)

    def is_removed(self, path):
        # ponytail: linear in removed selections, not drive records; use a path trie
        # if a session accumulates enough separate deletions to make this costly.
        return any(within(path, root) for root in tuple(self.removed))

    def affected(self, path):
        return any(within(path, root) or within(root, path)
                   for root in (*tuple(self.removed), *tuple(self.changed)))

    def notice(self):
        return (f'{self.files:,} file(s) and {self.folders:,} folder(s) confirmed removed. '
                'Folder contents are not included in these counts. '
                'Tree totals are from the last scan. Refresh for a fresh analysis.')

    def context(self):
        # Keep model input bounded even after a large multi-selection delete.
        from itertools import islice
        return {'notice': self.notice(), 'removed_paths': list(islice(self.removed.values(), 16)),
                'changed_paths': list(islice(self.changed, 16)),
                'paths_partial': len(self.removed) > 16 or len(self.changed) > 16,
                'guidance': 'Removed folders include their descendants. Changed folders may be partly changed. '
                            'Search and totals still describe the old scan. Tool items are marked when affected. '
                            'Do not offer removed or changed paths as current cleanup actions.'}
