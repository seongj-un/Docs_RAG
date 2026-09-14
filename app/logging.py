"""Logging owned by the application: format, level, and the request id.

Until this module existed the app configured no logging at all. What actually
installed the root handler was ``alembic.ini``: startup called
``run_migrations()`` (``app/main.py``), which reached ``fileConfig()`` in
``alembic/env.py``, whose ``[logger_root]`` section then governed every line
the server printed for the rest of the process. A migration tool's ini file
was the runtime observability policy of the API.

That coupling has already cost us once (3988fc9). ``fileConfig`` defaults to
``disable_existing_loggers=True``, so the startup migration disabled uvicorn's
loggers wholesale: from M1 through M6 the server produced no access log and no
traceback for a 500, and a smoke failure took two restarts to diagnose because
the server had nothing to say. One keyword argument stopped the bleeding, but
the cause stayed: ``fileConfig`` also flushes and closes *every* handler in the
process (``logging/config.py``), so the next person to send logs to a file
would lose them at startup with no error. ``app/db.py`` now tells
``alembic/env.py`` to leave logging alone when the app calls it programmatically
(``configure_logger``), and this module owns it instead. The operator's
``alembic upgrade head`` on the command line still configures itself from the
ini, because there is no app there to do it.
"""

import logging
import re
import sys
import time
import uuid
from contextvars import ContextVar

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import settings

REQUEST_ID_HEADER = "X-Request-Id"
_REQUEST_ID_HEADER_BYTES = REQUEST_ID_HEADER.lower().encode("latin-1")

# Lines with no request behind them — startup, migrations, background work —
# still need to fill the column, and "-" reads as "no request" rather than as
# a field that went missing.
NO_REQUEST_ID = "-"

request_id: ContextVar[str] = ContextVar("request_id", default=NO_REQUEST_ID)

# Timestamps are UTC and say so with a trailing Z. Every instant this system
# stores is timestamptz in UTC while the operator lives in KST, so a log line
# without a zone would be a third convention to convert between; the offset is
# spelled out instead of assumed. Sortable prefix first for machines, then a
# fixed-width level, the logger name, and the request id in brackets so a
# single grep for an id pulls a whole request out of the stream.
_FORMAT = (
    "%(asctime)s.%(msecs)03dZ %(levelname)-8s %(name)s "
    "[%(request_id)s] %(message)s"
)
_DATEFMT = "%Y-%m-%dT%H:%M:%S"

# Marks the handler and the record factory this module installed, so
# re-running setup is a no-op no matter how it is reached (uvicorn --reload
# re-imports, the test suite drives the lifespan more than once). A module
# level flag would lie to anyone who tore the handler off the root logger
# again, which is exactly what the tests do.
_MARK = "_docs_rag_logging"

# An inbound id is a correlation hint from a proxy, never an identity or a
# capability, so the only question is whether it is safe to echo and to write
# into a log line. It is not trusted as-is: the value is printed into every
# record of the request and returned in a response header, where a newline
# would forge log lines and a control character could carry terminal escapes.
# Anything outside this alphabet, or longer than a comfortable id, is dropped
# and replaced with one we generated -- dropping loses a hop of correlation,
# accepting loses the integrity of the log itself.
_ACCEPTABLE_ID = re.compile(r"[A-Za-z0-9._:@=+-]{1,64}")


def _resolve_level(name: str) -> tuple[int, str | None]:
    """Map ``LOG_LEVEL`` to a level number, tolerating a typo."""
    level = logging.getLevelNamesMapping().get(name.strip().upper())
    if level is None:
        # A misspelled level must not stop the server from booting. The whole
        # point of this module is that logging is infrastructure the app owns;
        # infrastructure that refuses to start over a typo in an unrelated
        # setting is worse than infrastructure that falls back and says so.
        return logging.WARNING, name
    return level, None


def configure_logging() -> None:
    """Install the app's root handler, format, level and record factory.

    Called first thing in the lifespan, before the startup migration, so that
    every line a booting process prints has the same shape. Before this,
    ``warn_about_mail_configuration()`` ran ahead of the migration and its
    warning went out through the stdlib's ``lastResort`` handler (bare text, no
    level, no name) while everything after it went out through alembic's
    handler -- the same logger changed appearance halfway through startup.
    """
    root = logging.getLogger()
    if any(getattr(handler, _MARK, False) for handler in root.handlers):
        return

    level, unknown = _resolve_level(settings.log_level)

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)
    formatter.converter = time.gmtime  # the Z in the format has to be true

    # stderr, where uvicorn also writes: stdout is where a process's output is
    # expected, and splitting diagnostics across both streams is what made
    # "the app log" and "the access log" two different places to look.
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    setattr(handler, _MARK, True)

    # Added, not swapped in: clearing the root handlers would be this module
    # doing to everyone else exactly what fileConfig did to us.
    root.addHandler(handler)
    root.setLevel(level)

    # Keep what alembic.ini's [logger_alembic] used to give us. Startup runs
    # migrations, and alembic announces at INFO which revision it is applying;
    # with the root default at WARNING those lines would vanish, so taking
    # logging away from the ini would quietly cost the one thing the ini was
    # actually contributing -- a deploy would no longer say what it migrated.
    logging.getLogger("alembic").setLevel(logging.INFO)

    access = logging.getLogger("uvicorn.access")
    # Idempotent like the rest of this function. The early return above guards
    # the usual re-entry, but the tests tear the handler off and call again, and
    # a filter stacked twice would be invisible until someone read the list.
    if not any(isinstance(f, _HealthCheckNoise) for f in access.filters):
        access.addFilter(_HealthCheckNoise())

    _install_record_factory()

    if unknown is not None:
        logging.getLogger(__name__).warning(
            "LOG_LEVEL=%r is not a level name; falling back to WARNING", unknown
        )


class _HealthCheckNoise(logging.Filter):
    """Drop the access line for health checks that passed.

    The container healthcheck hits ``/health`` every 30 seconds (Dockerfile,
    and the same for the models and web images), so an idle box writes about
    2,880 identical access lines a day. That is the bulk of the access log on
    any day nobody uses the service, and it pushes the lines that matter out of
    whatever window an operator scrolls back through -- and out of the 10MB the
    log driver now keeps (docker-compose.yml).

    Only the *successful* ones are dropped. A health check that fails is the
    moment the line earns its place: it is the difference between "the
    container is being restarted" and "the container is fine and something
    upstream is wrong", and ``docker inspect`` only keeps the last few.

    Reads uvicorn's positional args rather than the formatted message, because
    formatting has not happened yet at filter time. The shape is uvicorn's
    (client, method, path, http_version, status); anything that does not match
    it is left alone -- a filter that swallows records it failed to understand
    is the kind of thing that costs a diagnosis later.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        path, status = args[2], args[4]
        if not isinstance(path, str) or not isinstance(status, int):
            return True
        return not (path.split("?", 1)[0] == "/health" and status < 400)


def _install_record_factory() -> None:
    """Stamp every record with the request id of the request that caused it.

    A ``logging.Filter`` on our handler was the alternative and was rejected:
    a filter only reaches records that pass through the handler it is attached
    to, so uvicorn's own loggers (their own handlers, ``propagate=False``) and
    pytest's ``caplog`` would see records with no id, and every handler added
    later would have to remember to attach it. The factory runs where the
    record is created, so the attribute exists everywhere a record can go --
    including a ``%(request_id)s`` in someone else's format string.
    """
    previous = logging.getLogRecordFactory()
    if getattr(previous, _MARK, False):
        return

    def factory(*args, **kwargs) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        record.request_id = request_id.get()
        return record

    setattr(factory, _MARK, True)
    logging.setLogRecordFactory(factory)


def _inbound_request_id(scope: Scope) -> str | None:
    for key, value in scope.get("headers", ()):
        if key == _REQUEST_ID_HEADER_BYTES:
            # latin-1 is the wire encoding of a header; it cannot fail, so a
            # hostile byte becomes a character the pattern below rejects
            # rather than an exception on a request that was otherwise fine.
            candidate = value.decode("latin-1").strip()
            return candidate if _ACCEPTABLE_ID.fullmatch(candidate) else None
    return None


class RequestIdMiddleware:
    """Give every request an id, carry it in a contextvar, return it.

    Nothing tied the four places a single request shows up -- Caddy's access
    log, uvicorn's access log, the app's own log, and the ``traces`` row --
    together. An SSE failure logged a conversation id and nothing else, and a
    conversation id does not identify the turn that failed.

    Written against raw ASGI rather than as a ``BaseHTTPMiddleware``. That base
    class runs the endpoint in a child task and re-wraps the response, which is
    a poor fit for the streaming endpoint this exists to explain
    (``routers/conversations.py`` yields SSE events one token at a time). Plain
    ASGI also keeps the contextvar set for the entire response, so the log
    calls inside the stream -- the ones that fire long after the headers went
    out -- still carry the id.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # uuid4().hex, not uuid4(): no dashes keeps it one grep token.
        current = _inbound_request_id(scope) or uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = current
        token = request_id.set(current)

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = current
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            # Reset even when the app raised: the same task serves the next
            # request under uvicorn, and a leaked id would attach this
            # request's key to an unrelated one -- a wrong correlation is
            # worse than none, because it is believed.
            request_id.reset(token)
