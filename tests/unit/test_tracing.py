"""The tracing seam (backend/tracing.py): off by default, local hosts only, and it never breaks a run."""
import logging
import time
from uuid import uuid4

import pytest

from backend import tracing
from backend.tracing import Trace, is_local_host, start_trace

ON = {"LANGFUSE_BASE_URL": "http://127.0.0.1:13001", "LANGFUSE_PUBLIC_KEY": "pk-lf-fake", "LANGFUSE_SECRET_KEY": "sk-lf-fake"}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("LANGFUSE_BASE_URL", "LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)


def configure(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def never_built(*a, **k):
    raise AssertionError("the Langfuse SDK must not be touched")


# ---- off by default ----

def test_no_settings_means_no_callbacks_and_no_trace_id(monkeypatch):
    monkeypatch.setattr(tracing, "_build", never_built)
    t = start_trace(uuid4())
    assert t.callbacks == [] and t.trace_id is None
    t.flush()  # a no-op, not an error


@pytest.mark.parametrize("missing", ["LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"])
def test_one_missing_setting_keeps_tracing_off(monkeypatch, missing):
    monkeypatch.setattr(tracing, "_build", never_built)
    configure(monkeypatch, **{k: v for k, v in ON.items() if k != missing})
    assert start_trace(uuid4()).callbacks == []


def test_the_older_host_name_still_switches_it_on(monkeypatch):
    seen = {}
    monkeypatch.setattr(tracing, "_build", lambda host, pk, sk, tid: seen.update(host=host) or Trace(["h"], tid))
    configure(monkeypatch, LANGFUSE_HOST="http://localhost:3000", LANGFUSE_PUBLIC_KEY="p", LANGFUSE_SECRET_KEY="s")
    assert start_trace(uuid4()).callbacks == ["h"] and seen["host"] == "http://localhost:3000"


# ---- local hosts only ----

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:13001", "http://localhost:3000", "http://[::1]:3000", "http://langfuse-web:3000",
    "https://127.0.0.1",
])
def test_local_hosts_are_accepted(url):
    assert is_local_host(url)


@pytest.mark.parametrize("url", [
    "https://cloud.langfuse.com", "https://us.cloud.langfuse.com", "http://evil.example",
    "http://127.0.0.1@evil.example",       # user-info trick: the real host is evil.example
    "http://localhost.evil.example",       # prefix trick
    "http://127.0.0.1.evil.example",
    "127.0.0.1:3000",                      # no scheme
    "ftp://127.0.0.1", "", "http://",
])
def test_other_hosts_are_refused(url):
    assert not is_local_host(url)


def test_a_non_local_host_refuses_to_enable_and_never_touches_the_sdk(monkeypatch, caplog):
    monkeypatch.setattr(tracing, "_build", never_built)
    configure(monkeypatch, **{**ON, "LANGFUSE_BASE_URL": "https://cloud.langfuse.com"})
    with caplog.at_level(logging.WARNING, logger="backend.tracing"):
        t = start_trace(uuid4())
    assert t.callbacks == [] and t.trace_id is None
    assert "refusing to enable" in caplog.text


def test_the_host_checked_is_the_host_the_sdk_would_use(monkeypatch):
    """The SDK reads BASE_URL before HOST. A local HOST must not cover for a hosted BASE_URL."""
    monkeypatch.setattr(tracing, "_build", never_built)
    configure(monkeypatch, **{**ON, "LANGFUSE_BASE_URL": "https://cloud.langfuse.com", "LANGFUSE_HOST": "http://127.0.0.1:3000"})
    assert start_trace(uuid4()).callbacks == []


# ---- fail open ----

def test_a_setup_error_gives_an_untraced_run_and_a_warning(monkeypatch, caplog):
    def boom(*a):
        raise RuntimeError("langfuse exploded")

    monkeypatch.setattr(tracing, "_build", boom)
    configure(monkeypatch, **ON)
    with caplog.at_level(logging.WARNING, logger="backend.tracing"):
        t = start_trace(uuid4())
    assert t.callbacks == [] and t.trace_id is None
    assert "could not start" in caplog.text and "langfuse exploded" in caplog.text


def test_a_missing_package_is_a_setup_error_not_a_crash(monkeypatch, caplog):
    """Until 4.2 adds langfuse to the dependencies, switching tracing on must still leave runs working."""
    try:
        import langfuse  # noqa: F401
        pytest.skip("langfuse is installed here; the dead-host test below covers this path")
    except ImportError:
        pass
    configure(monkeypatch, **ON)
    with caplog.at_level(logging.WARNING, logger="backend.tracing"):
        assert start_trace(uuid4()).callbacks == []
    assert "could not start" in caplog.text


def test_when_on_the_trace_id_is_the_run_id_as_32_hex(monkeypatch):
    monkeypatch.setattr(tracing, "_build", lambda host, pk, sk, tid: Trace(["h"], tid))
    configure(monkeypatch, **ON)
    run_id = uuid4()
    t = start_trace(run_id)
    assert t.trace_id == run_id.hex and len(t.trace_id) == 32


def test_a_failing_flush_is_a_warning_not_an_error(caplog):
    def boom():
        raise OSError("connection refused")

    with caplog.at_level(logging.WARNING, logger="backend.tracing"):
        Trace(["h"], "a" * 32, boom).flush()
    assert "flush failed" in caplog.text


def test_a_hanging_flush_cannot_hold_the_worker(monkeypatch, caplog):
    monkeypatch.setattr(tracing, "TIMEOUT_S", 0.2)
    t = Trace(["h"], "a" * 32, lambda: time.sleep(5))
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="backend.tracing"):
        t.flush()
    assert time.monotonic() - started < 2
    assert "not waiting" in caplog.text


def test_the_real_sdk_against_a_dead_local_port_fails_open(monkeypatch, caplog):
    """Only where the langfuse package is installed (not in the project environment until 4.2)."""
    pytest.importorskip("langfuse")
    configure(monkeypatch, **{**ON, "LANGFUSE_BASE_URL": "http://127.0.0.1:1"})
    with caplog.at_level(logging.WARNING, logger="backend.tracing"):
        t = start_trace(uuid4())
        started = time.monotonic()
        t.flush()
    assert len(t.callbacks) == 1 and t.trace_id
    assert time.monotonic() - started < tracing.TIMEOUT_S + 2
