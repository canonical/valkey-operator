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


def test_vm_exec_stream_streams_stdout_and_collects_stderr():
    from src.workload_vm import ValkeyVmWorkload

    workload = ValkeyVmWorkload.__new__(ValkeyVmWorkload)
    handle = workload.exec_stream(
        ["sh", "-c", "printf 'hello-stream'; printf 'oops' 1>&2"]
    )
    body = handle.stdout.read()
    rc, stderr = handle.wait()
    assert body == b"hello-stream"
    assert rc == 0
    assert "oops" in stderr


def test_vm_exec_stream_kill_terminates():
    from src.workload_vm import ValkeyVmWorkload

    workload = ValkeyVmWorkload.__new__(ValkeyVmWorkload)
    handle = workload.exec_stream(["sh", "-c", "sleep 30"])
    handle.kill()
    rc, _ = handle.wait()
    assert rc != 0
