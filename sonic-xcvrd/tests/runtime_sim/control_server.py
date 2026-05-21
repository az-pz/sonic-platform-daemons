"""
ControlServer - JSON-over-Unix-domain-socket control plane for the simulator.

Listens on ``/var/run/sonic_xcvr_sim.sock`` by default and dispatches one of
six explicitly enumerated operations against the shared :class:`SimState`.
Untrusted callers cannot trigger arbitrary code: payloads are parsed with
``json.loads`` and dispatched through a static dict of handlers - there is
no ``eval``, no ``exec`` and no shell invocation anywhere in this module.

Wire protocol
-------------

* Each client connection is one request, one response, one disconnect.
* The request is a single line of JSON (newline-terminated) up to
  ``MAX_MSG_BYTES`` bytes - oversized payloads are rejected.
* The response is a single line of JSON: ``{"ok": bool, ...}``.

Security
--------

* Socket file is unlinked on startup if stale.
* File mode is set to 0o660 immediately after ``bind`` and before ``listen``,
  so only members of the configured group may connect.
* No reflection of caller-supplied data is performed beyond what the handler
  explicitly returns.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
from typing import Any, Callable, Dict, Optional

from .sim_state import SimState

DEFAULT_SOCKET_PATH = "/var/run/sonic_xcvr_sim.sock"
MAX_MSG_BYTES = 4096
SOCKET_FILE_MODE = 0o660
SOCKET_BACKLOG = 8


class ControlServer:
    """Thread-based UDS server. Call :meth:`start` then :meth:`stop`."""

    def __init__(
        self,
        sim: SimState,
        socket_path: str = DEFAULT_SOCKET_PATH,
        socket_mode: int = SOCKET_FILE_MODE,
    ) -> None:
        self._sim = sim
        self._socket_path = socket_path
        self._socket_mode = socket_mode
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
            "insert": self._op_insert,
            "remove": self._op_remove,
            "error": self._op_error,
            "dom_set": self._op_dom_set,
            "list": self._op_list,
            "ping": self._op_ping,
        }

    # ----- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        # Unlink any stale socket from a previous crash before binding.
        try:
            existing = os.lstat(self._socket_path)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISSOCK(existing.st_mode):
                os.unlink(self._socket_path)
            else:
                raise RuntimeError(
                    "refusing to overwrite non-socket file at {}".format(self._socket_path)
                )

        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self._socket_path)
        os.chmod(self._socket_path, self._socket_mode)
        self._sock.listen(SOCKET_BACKLOG)
        self._sock.settimeout(0.5)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._accept_loop, name="xcvr-sim-uds", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        try:
            os.unlink(self._socket_path)
        except FileNotFoundError:
            pass

    # ----- internals -------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._handle_connection(conn)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _handle_connection(self, conn: socket.socket) -> None:
        conn.settimeout(2.0)
        chunks = []
        total = 0
        # Read until newline, bounded by MAX_MSG_BYTES.
        while True:
            try:
                chunk = conn.recv(min(1024, MAX_MSG_BYTES - total + 1))
            except socket.timeout:
                self._send(conn, {"ok": False, "error": "timeout"})
                return
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_MSG_BYTES:
                self._send(conn, {"ok": False, "error": "request too large"})
                return
            if b"\n" in chunk:
                break
        raw = b"".join(chunks).split(b"\n", 1)[0]
        if not raw:
            self._send(conn, {"ok": False, "error": "empty request"})
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send(conn, {"ok": False, "error": "invalid JSON: {}".format(exc)})
            return
        if not isinstance(payload, dict):
            self._send(conn, {"ok": False, "error": "payload must be an object"})
            return
        op = payload.get("op")
        handler = self._handlers.get(op) if isinstance(op, str) else None
        if handler is None:
            self._send(conn, {"ok": False, "error": "unknown op"})
            return
        try:
            response = handler(payload)
        except (TypeError, ValueError, KeyError) as exc:
            response = {"ok": False, "error": str(exc)}
        self._send(conn, response)

    @staticmethod
    def _send(conn: socket.socket, payload: Dict[str, Any]) -> None:
        data = (json.dumps(payload) + "\n").encode("utf-8")
        try:
            conn.sendall(data)
        except OSError:
            pass

    # ----- op handlers -----------------------------------------------------

    @staticmethod
    def _require_int(payload: Dict[str, Any], key: str) -> int:
        value = payload.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("{!r} must be an integer".format(key))
        return value

    @staticmethod
    def _require_str(payload: Dict[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError("{!r} must be a non-empty string".format(key))
        return value

    def _op_insert(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        port = self._require_int(payload, "port")
        profile = payload.get("profile", "qsfp28-100g-cwdm4")
        if not isinstance(profile, str):
            raise ValueError("'profile' must be a string")
        self._sim.insert(port, profile)
        return {"ok": True, "port": port, "profile": profile}

    def _op_remove(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        port = self._require_int(payload, "port")
        self._sim.remove(port)
        return {"ok": True, "port": port}

    def _op_error(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        port = self._require_int(payload, "port")
        code = self._require_str(payload, "code")
        self._sim.set_error(port, code)
        return {"ok": True, "port": port, "code": code}

    def _op_dom_set(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        port = self._require_int(payload, "port")
        field = self._require_str(payload, "field")
        value = payload.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("'value' must be numeric")
        self._sim.dom_set(port, field, float(value))
        return {"ok": True, "port": port, "field": field, "value": float(value)}

    def _op_list(self, _payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True, "state": self._sim.list_state()}

    def _op_ping(self, _payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True, "pong": True}
