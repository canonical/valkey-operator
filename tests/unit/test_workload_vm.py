#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for ValkeyVmWorkload.total_memory_bytes per-unit RAM detection."""

import workload_vm
from workload_vm import ValkeyVmWorkload


class _FakeFile:
    """Stand-in for a pathops path: read_text returns content or raises."""

    def __init__(self, content):
        self._content = content

    def read_text(self) -> str:
        if isinstance(self._content, Exception):
            raise self._content
        return self._content


class _FakeRoot:
    """Stand-in for ValkeyVmWorkload.root_dir.

    Maps a relative path (str) to file content; unknown paths raise
    FileNotFoundError, mimicking an absent file on disk.
    """

    def __init__(self, files: dict):
        self._files = files

    def __truediv__(self, relative_path: str) -> _FakeFile:
        if relative_path in self._files:
            return _FakeFile(self._files[relative_path])
        return _FakeFile(FileNotFoundError(relative_path))


def _vm_workload(files: dict) -> ValkeyVmWorkload:
    """Build a ValkeyVmWorkload without running __init__ (which needs snapd).

    total_memory_bytes only reads through self.root_dir, so injecting a fake
    root is sufficient.
    """
    workload = object.__new__(ValkeyVmWorkload)
    workload.root_dir = _FakeRoot(files)
    return workload


def test_memory_max_numeric_is_returned():
    """A hard cgroup limit (LXD limits.memory) is the per-unit budget."""
    workload = _vm_workload({"sys/fs/cgroup/memory.max": "536870912\n"})
    assert workload.total_memory_bytes() == 536870912


def test_falls_back_to_memory_high_when_max_is_unset():
    """LXD soft-enforce leaves memory.max=max and sets memory.high."""
    workload = _vm_workload(
        {
            "sys/fs/cgroup/memory.max": "max\n",
            "sys/fs/cgroup/memory.high": "268435456\n",
        }
    )
    assert workload.total_memory_bytes() == 268435456


def test_falls_back_to_meminfo_when_no_cgroup_limit():
    """No cgroup limit -> /proc/meminfo MemTotal (KiB) converted to bytes."""
    workload = _vm_workload(
        {
            "sys/fs/cgroup/memory.max": "max\n",
            "sys/fs/cgroup/memory.high": "max\n",
            "proc/meminfo": "MemTotal:       2048 kB\nMemFree:         100 kB\n",
        }
    )
    assert workload.total_memory_bytes() == 2048 * 1024


def test_meminfo_used_when_cgroup_files_absent():
    """Absent cgroup files (FileNotFoundError) fall through to meminfo."""
    workload = _vm_workload({"proc/meminfo": "MemTotal: 4096 kB\n"})
    assert workload.total_memory_bytes() == 4096 * 1024


def test_garbage_cgroup_value_falls_through_to_meminfo():
    """A non-integer, non-'max' cgroup value is ignored, not returned."""
    workload = _vm_workload(
        {
            "sys/fs/cgroup/memory.max": "not-a-number\n",
            "proc/meminfo": "MemTotal: 1024 kB\n",
        }
    )
    assert workload.total_memory_bytes() == 1024 * 1024


def test_returns_zero_when_nothing_readable():
    """All reads raise FileNotFoundError -> 0 (gate treats as below threshold)."""
    workload = _vm_workload({})
    assert workload.total_memory_bytes() == 0


def test_meminfo_without_memtotal_line_returns_zero():
    """A meminfo without a MemTotal line returns 0."""
    workload = _vm_workload(
        {
            "sys/fs/cgroup/memory.max": "max\n",
            "sys/fs/cgroup/memory.high": "max\n",
            "proc/meminfo": "MemFree: 100 kB\n",
        }
    )
    assert workload.total_memory_bytes() == 0


def test_sysconf_path_is_gone():
    """Regression guard: total_memory_bytes no longer reads RAM via os.sysconf.

    ``import os`` may legitimately reappear for unrelated reasons (e.g. an
    ``os.environ`` use elsewhere in the module), so guard against the sysconf
    call itself rather than the mere presence of the ``os`` module.
    """
    import inspect

    assert "sysconf" not in inspect.getsource(workload_vm)
