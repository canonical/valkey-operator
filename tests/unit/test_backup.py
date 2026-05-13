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


def test_k8s_exec_stream_delegates_to_container_exec(mocker):
    from src.workload_k8s import ValkeyK8sWorkload

    container = mocker.MagicMock()
    fake_process = mocker.MagicMock()
    fake_process.stdout = b""
    container.exec.return_value = fake_process

    workload = ValkeyK8sWorkload(container=container)
    handle = workload.exec_stream(["valkey-cli", "--rdb", "-"])

    container.exec.assert_called_once_with(
        command=["valkey-cli", "--rdb", "-"],
        encoding=None,
        timeout=None,
    )
    assert handle.stdout is fake_process.stdout


def test_k8s_exec_stream_wait_returns_rc_and_stderr(mocker):
    from ops.pebble import ExecError

    from src.workload_k8s import ValkeyK8sWorkload

    container = mocker.MagicMock()
    fake_process = mocker.MagicMock()
    # wait_output() on a successful binary process returns (stdout_bytes, stderr_bytes)
    fake_process.wait_output.return_value = (b"", b"oops")
    container.exec.return_value = fake_process

    workload = ValkeyK8sWorkload(container=container)
    handle = workload.exec_stream(["true"])
    rc, stderr = handle.wait()

    assert rc == 0
    assert stderr == "oops"

    # Failing exec
    fake_process.wait_output.side_effect = ExecError(
        command=["false"], exit_code=2, stdout=b"", stderr=b"boom"
    )
    handle = workload.exec_stream(["false"])
    rc, stderr = handle.wait()
    assert rc == 2
    assert stderr == "boom"
