#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the workload streaming exec primitive."""


def test_workload_base_declares_exec_stream():
    import inspect

    from src.core.base_workload import ProcessHandle, WorkloadBase

    assert hasattr(WorkloadBase, "exec_stream")
    annotations = (
        ProcessHandle.__annotations__ if hasattr(ProcessHandle, "__annotations__") else {}
    )
    assert "stdout" in annotations
    members = {name for name, _ in inspect.getmembers(ProcessHandle)}
    assert "wait" in members
    assert "kill" in members
