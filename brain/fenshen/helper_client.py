"""Client for the on-device fenshen-helper JSON RPC (see helper/src/com/fenshen/helper/RpcServer.java)."""

from __future__ import annotations

import json
import socket
import threading
from typing import Any


class HelperError(RuntimeError):
    pass


class ConflictError(HelperError):
    """The target app is open on the user's screen; launching it would steal it from them."""


class HelperClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 27183, timeout: float = 30.0):
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._file = self._sock.makefile("rwb")
        self._next_id = 0
        self._lock = threading.Lock()

    def call(self, cmd: str, **args: Any) -> dict:
        with self._lock:
            self._next_id += 1
            request = {"id": self._next_id, "cmd": cmd, "args": args}
            self._file.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
            self._file.flush()
            line = self._file.readline()
        if not line:
            raise HelperError("helper closed the connection")
        response = json.loads(line.decode("utf-8"))
        if not response.get("ok"):
            error = response.get("error", "unknown error")
            if "CONFLICT:" in error:
                raise ConflictError(error)
            raise HelperError(error)
        return response["result"]

    def close(self) -> None:
        try:
            self._file.close()
        finally:
            self._sock.close()
