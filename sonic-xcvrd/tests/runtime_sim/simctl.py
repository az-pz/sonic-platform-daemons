"""
xcvrd_simctl - thin client for the runtime transceiver simulator.

Invocation::

    python -m tests.runtime_sim.simctl insert 5 --profile qsfp28-100g-cwdm4
    python -m tests.runtime_sim.simctl remove 5
    python -m tests.runtime_sim.simctl error  5 blocking
    python -m tests.runtime_sim.simctl dom    5 temperature 75.0
    python -m tests.runtime_sim.simctl list

Exit codes:

* 0 on ``{"ok": true}``
* 1 on ``{"ok": false}`` or transport errors
* 2 on argument errors

This module is also the body of the planned ``xcvrd_simctl`` console script
entry point in the external package.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from typing import Any, Dict, Optional

from .control_server import DEFAULT_SOCKET_PATH, MAX_MSG_BYTES


def _send(socket_path: str, payload: Dict[str, Any], timeout: float = 5.0) -> Dict[str, Any]:
    encoded = (json.dumps(payload) + "\n").encode("utf-8")
    if len(encoded) > MAX_MSG_BYTES:
        raise ValueError("request exceeds {} bytes".format(MAX_MSG_BYTES))
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(socket_path)
        sock.sendall(encoded)
        chunks = []
        total = 0
        while True:
            chunk = sock.recv(1024)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_MSG_BYTES:
                raise ValueError("response exceeds {} bytes".format(MAX_MSG_BYTES))
            if b"\n" in chunk:
                break
    finally:
        sock.close()
    raw = b"".join(chunks).split(b"\n", 1)[0]
    return json.loads(raw.decode("utf-8"))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xcvrd_simctl", description=__doc__.splitlines()[1])
    p.add_argument(
        "--socket", default=DEFAULT_SOCKET_PATH,
        help="Path to the simulator UDS (default: %(default)s)",
    )
    sub = p.add_subparsers(dest="op", required=True)

    sp = sub.add_parser("insert", help="insert a transceiver")
    sp.add_argument("port", type=int)
    sp.add_argument("--profile", default="qsfp28-100g-cwdm4")

    sp = sub.add_parser("remove", help="remove a transceiver")
    sp.add_argument("port", type=int)

    sp = sub.add_parser("error", help="raise an SFP error for a port")
    sp.add_argument("port", type=int)
    sp.add_argument("code")

    sp = sub.add_parser("dom", help="set a DOM field")
    sp.add_argument("port", type=int)
    sp.add_argument("field")
    sp.add_argument("value", type=float)

    sub.add_parser("list", help="show the simulated presence table")
    sub.add_parser("ping", help="connectivity check")
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    request: Dict[str, Any]
    if args.op == "insert":
        request = {"op": "insert", "port": args.port, "profile": args.profile}
    elif args.op == "remove":
        request = {"op": "remove", "port": args.port}
    elif args.op == "error":
        request = {"op": "error", "port": args.port, "code": args.code}
    elif args.op == "dom":
        request = {"op": "dom_set", "port": args.port, "field": args.field, "value": args.value}
    elif args.op == "list":
        request = {"op": "list"}
    elif args.op == "ping":
        request = {"op": "ping"}
    else:
        return 2

    try:
        response = _send(args.socket, request)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps(response))
    return 0 if response.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
