"""The tracing seam: the one place that knows about Langfuse. Off unless configured, and it never breaks a run.

    trace = start_trace(run_id)
    graph.invoke(state, config={"callbacks": trace.callbacks})
    trace.flush()

Langfuse (self-hosted, WBS 4.2) is the project's tracer. LangSmith is hosted and would receive invoice text; it is a
transitive dependency of langgraph and stays disabled (LANGSMITH_TRACING=false, see docs/decisions.md).

On only when a host and both keys are set. The host is read like the SDK reads it (LANGFUSE_BASE_URL first, then the
older LANGFUSE_HOST), so the value checked here is the value used. It must be this machine or the self-hosted service on
custodian-net; anything else is refused. Keys come from .env and are never committed.

Fail open: a problem in here is logged as a warning and the run goes on untraced. The `langfuse` package is imported
only when tracing is on and is not a dependency yet; 4.2 adds `langfuse==5.0.4` (current SDK, checked 2026-10-22:
`from langfuse.langchain import CallbackHandler`).

PII: traces contain invoice text. Mask it (Presidio, WBS 3.1) BEFORE anything reaches traces or logs. Until then
tracing is for synthetic data only.
"""
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlparse
from uuid import UUID

log = logging.getLogger(__name__)

# Where a trace may go: this machine, or the self-hosted Langfuse web service on custodian-net (added in 4.2).
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "langfuse-web"})
TIMEOUT_S = 2.0  # the SDK's own default is 5 s; also the longest a flush may hold the worker


@dataclass
class Trace:
    callbacks: list = field(default_factory=list)  # pass as config={"callbacks": ...}; empty when tracing is off
    trace_id: str | None = None                    # stored on agent.runs.trace_id when tracing is on
    _flush: Callable[[], None] | None = None

    def flush(self) -> None:
        """Send what is buffered, for at most TIMEOUT_S. Never raises. Call it after the run is saved."""
        if self._flush is None:
            return

        def work() -> None:
            try:
                self._flush()
            except Exception as e:  # noqa: BLE001  fail open: a flush problem never reaches the run
                log.warning("tracing: flush failed (%s: %s); the run is unaffected", type(e).__name__, e)

        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(TIMEOUT_S)
        if t.is_alive():
            log.warning("tracing: flush still running after %.0f s; not waiting for it", TIMEOUT_S)


def is_local_host(url: str) -> bool:
    """True for http(s) URLs whose host is this machine or the self-hosted service. `.hostname` ignores user-info,
    so http://127.0.0.1@evil.example is judged as evil.example."""
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and p.hostname in LOCAL_HOSTS
    except ValueError:
        return False


def _settings() -> tuple[str, str, str] | None:
    host = os.getenv("LANGFUSE_BASE_URL") or os.getenv("LANGFUSE_HOST")
    public, secret = os.getenv("LANGFUSE_PUBLIC_KEY"), os.getenv("LANGFUSE_SECRET_KEY")
    return (host, public, secret) if host and public and secret else None


def _build(host: str, public: str, secret: str, trace_id: str) -> Trace:
    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler

    client = Langfuse(public_key=public, secret_key=secret, base_url=host, timeout=TIMEOUT_S)
    handler = CallbackHandler(public_key=public, trace_context={"trace_id": trace_id})
    return Trace([handler], trace_id, client.flush)


def start_trace(run_id: UUID) -> Trace:
    """The callbacks for one graph run. An empty Trace (no callbacks, no trace id) means tracing is off."""
    settings = _settings()
    if settings is None:
        return Trace()
    host, public, secret = settings
    if not is_local_host(host):
        log.warning("tracing: refusing to enable, the Langfuse host must be this machine or custodian-net")
        return Trace()
    try:
        return _build(host, public, secret, run_id.hex)  # 32 lowercase hex: what Langfuse accepts as a trace id
    except Exception as e:  # noqa: BLE001  missing package, bad config, anything: the run goes on untraced
        log.warning("tracing: could not start (%s: %s); the run goes on untraced", type(e).__name__, e)
        return Trace()
