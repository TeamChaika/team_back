"""Best effort bounded asynchronous capture; support never blocks business requests."""

import atexit
import logging
import queue
import re
import threading
from pathlib import Path
from uuid import uuid4

log = logging.getLogger(__name__)
_APP = Path(__file__).resolve().parents[1]
_CODE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")
_default = None


def diagnostic(exc):
    if exc is None:
        return {}
    name = type(exc).__name__
    frames = []
    trace = exc.__traceback__
    while trace:
        code = trace.tb_frame.f_code
        try:
            module = Path(code.co_filename).resolve().relative_to(_APP)
        except ValueError:
            trace = trace.tb_next
            continue
        function = code.co_name
        if _CODE.fullmatch(function) and all(
            _CODE.fullmatch(p) for p in module.with_suffix("").parts
        ):
            frames.append(
                {
                    "module": "app." + ".".join(module.with_suffix("").parts),
                    "function": function,
                    "line": trace.tb_lineno,
                }
            )
        trace = trace.tb_next
    return {
        "exception_class": name if _CODE.fullmatch(name) else "Exception",
        "frames": frames[-8:],
    }


class Capture:
    def __init__(self, store):
        self.store = store
        self.queue = queue.Queue(maxsize=128)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name="support-capture", daemon=True)
        self._start_lock = threading.Lock()
        self._started = False
        atexit.register(self.close)

    def submit(
        self,
        context,
        *,
        source="server",
        code="http_5xx",
        screen="other",
        status_code=None,
        request_id=None,
        exception=None,
    ):
        if source not in {"server", "background"} or code not in {
            "http_5xx",
            "unhandled_exception",
            "sync_failed",
            "document_failed",
        }:
            return
        if self.stopped.is_set():
            return
        values = {
            "client_request_id": str(uuid4()),
            "screen": screen,
            "status_code": status_code,
            "request_id": str(request_id) if request_id else None,
            "diagnostic": diagnostic(exception),
        }
        try:
            self.queue.put_nowait((context, values, source, code))
            with self._start_lock:
                if not self._started:
                    self._started = True
                    self.thread.start()
        except queue.Full:
            pass

    def _run(self):
        while not self.stopped.is_set() or not self.queue.empty():
            try:
                item = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.store.event(*item)
            except Exception:
                # Fixed code only: never log DB errors or values containing secrets.
                log.warning("support_capture_unavailable")
            finally:
                self.queue.task_done()

    def close(self):
        self.stopped.set()
        if self._started and threading.current_thread() is not self.thread:
            self.thread.join(timeout=1.0)


def configure_capture(store, default_context=None):
    global _default
    capture = Capture(store)
    _default = (capture, default_context)
    return capture


def capture_background(code, *, exception=None):
    if _default and _default[1]:
        _default[0].submit(_default[1], source="background", code=code, exception=exception)


def capture(
    context,
    *,
    source="background",
    code,
    screen="other",
    status_code=None,
    request_id=None,
    exception=None,
):
    if _default:
        _default[0].submit(
            context,
            source=source,
            code=code,
            screen=screen,
            status_code=status_code,
            request_id=request_id,
            exception=exception,
        )
