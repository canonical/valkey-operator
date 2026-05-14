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
    handle = workload.exec_stream(["sh", "-c", "printf 'hello-stream'; printf 'oops' 1>&2"])
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
    import io

    from src.workload_k8s import ValkeyK8sWorkload

    container = mocker.MagicMock()
    fake_process = mocker.MagicMock()
    fake_process.stdout = io.BytesIO(b"")
    fake_process.stderr = io.BytesIO(b"")
    container.exec.return_value = fake_process

    workload = ValkeyK8sWorkload(container=container)
    handle = workload.exec_stream(["valkey-cli", "--rdb", "-"])

    container.exec.assert_called_once_with(
        command=["valkey-cli", "--rdb", "-"],
        encoding=None,
        timeout=None,
        environment=None,
    )
    assert handle.stdout is fake_process.stdout


def test_build_command_prefix_no_tls(mocker):
    from src.common.client import CliClient

    workload = mocker.MagicMock()
    workload.cli = "valkey-cli"
    client = CliClient(username="op", password="pw", tls=False, workload=workload)
    client.port = 6379
    prefix = client.build_command_prefix(json_output=True)
    assert prefix[0] == "valkey-cli"
    assert "--user" in prefix and "op" in prefix
    assert "--json" in prefix
    assert "--tls" not in prefix
    # The password must never reach argv; it goes via REDISCLI_AUTH.
    assert "--pass" not in prefix
    assert "pw" not in prefix


def test_exec_cli_command_passes_password_via_env(mocker):
    from src.common.client import CliClient

    workload = mocker.MagicMock()
    workload.cli = "valkey-cli"
    workload.exec.return_value = ("OK", None)
    client = CliClient(username="op", password="sekret", tls=False, workload=workload)
    client.exec_cli_command(["ping"], hostname="h", json_output=False)

    _args, kwargs = workload.exec.call_args
    assert kwargs["env"] == {"REDISCLI_AUTH": "sekret"}
    sent_argv = workload.exec.call_args[0][0]
    assert "--pass" not in sent_argv and "sekret" not in sent_argv


def test_build_command_prefix_with_tls(mocker):
    from src.common.client import CliClient

    workload = mocker.MagicMock()
    workload.cli = "valkey-cli"
    workload.tls_paths.client_cert.as_posix.return_value = "/c"
    workload.tls_paths.client_key.as_posix.return_value = "/k"
    workload.tls_paths.ca_certs_dir.as_posix.return_value = "/ca"
    client = CliClient(username="op", password="pw", tls=True, workload=workload)
    client.port = 6380
    prefix = client.build_command_prefix(json_output=False, hostname="h")
    assert "--tls" in prefix
    assert "--cert" in prefix and "/c" in prefix
    assert "--key" in prefix and "/k" in prefix
    assert "--cacertdir" in prefix and "/ca" in prefix
    assert "-h" in prefix and "h" in prefix
    assert "--json" not in prefix


def test_k8s_exec_stream_wait_streams_without_buffering_stdout(mocker):
    """wait() must call process.wait(), never wait_output() (which buffers
    the whole RDB into the charm container's memory)."""
    import io

    from ops.pebble import ExecError

    from src.workload_k8s import ValkeyK8sWorkload

    container = mocker.MagicMock()
    fake_process = mocker.MagicMock()
    fake_process.stdout = io.BytesIO(b"")
    fake_process.stderr = io.BytesIO(b"oops")
    fake_process.wait.return_value = None
    container.exec.return_value = fake_process

    workload = ValkeyK8sWorkload(container=container)
    handle = workload.exec_stream(["true"])
    rc, stderr = handle.wait()

    assert rc == 0
    assert stderr == "oops"
    fake_process.wait.assert_called_once()
    fake_process.wait_output.assert_not_called()

    # Failing exec: process.wait() raises ExecError carrying the exit code.
    fake_process2 = mocker.MagicMock()
    fake_process2.stdout = io.BytesIO(b"")
    fake_process2.stderr = io.BytesIO(b"boom")
    fake_process2.wait.side_effect = ExecError(
        command=["false"], exit_code=2, stdout=b"", stderr=b""
    )
    container.exec.return_value = fake_process2
    handle = workload.exec_stream(["false"])
    rc, stderr = handle.wait()
    assert rc == 2
    assert stderr == "boom"
