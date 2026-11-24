"""Frames of contracts C1 and C2: a 4-byte little-endian length N (1 to 16 MiB), then N bytes of UTF-8 JSON, one
object with a field `t`.

`Channel` wraps anything with `recv_into`/`recv` or `read` and `write`/`sendall`: a socket, a Windows pipe
(endpoint.PipeConn) or a process's binary pipes. Reads happen on one thread; writes may come from many and are made
whole under one lock, so the frames of different calls interleave but never mix.
"""

from __future__ import annotations

import json
import struct
import threading

MAX_FRAME = 16 * 1024 * 1024
PROTOCOL = 1


class FrameError(Exception):
    """A frame that breaks the framing or is not one JSON object with `t`: the connection cannot be trusted further."""


def encode(frame: dict) -> bytes:
    data = json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not 1 <= len(data) <= MAX_FRAME:
        raise FrameError(f"a frame of {len(data)} bytes")
    return struct.pack("<I", len(data)) + data


def decode(data: bytes) -> dict:
    try:
        frame = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FrameError(f"not UTF-8 JSON ({exc.__class__.__name__})") from None
    if not isinstance(frame, dict) or not isinstance(frame.get("t"), str):
        raise FrameError("not an object with t")
    return frame


class Channel:
    """Frames over a byte stream. `read()` returns the next frame, or None at the end of the stream."""

    def __init__(self, stream, *, writer=None, name: str = ""):
        self._in = stream
        self._out = writer if writer is not None else stream
        self._lock = threading.Lock()          # one whole frame at a time
        self._close_lock = threading.Lock()    # never the write lock: a send blocked on a full pipe must not hold a close
        self.name = name
        self.closed = False

    def _read_exact(self, size: int) -> bytes | None:
        chunks = []
        left = size
        while left:
            if hasattr(self._in, "recv"):
                chunk = self._in.recv(min(left, 1 << 20))
            else:
                chunk = self._in.read(left) if not hasattr(self._in, "read1") else self._in.read1(left)
            if not chunk:
                if left == size:
                    return None
                raise FrameError("the stream ended inside a frame")
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks)

    def read(self) -> dict | None:
        head = self._read_exact(4)
        if head is None:
            return None
        (size,) = struct.unpack("<I", head)
        if not 1 <= size <= MAX_FRAME:
            raise FrameError(f"a frame length of {size}")
        body = self._read_exact(size)
        if body is None:
            raise FrameError("the stream ended inside a frame")
        return decode(body)

    def send(self, frame: dict) -> None:
        data = encode(frame)
        with self._lock:
            if self.closed:
                raise BrokenPipeError("channel closed")
            if hasattr(self._out, "sendall"):
                self._out.sendall(data)
            else:                       # a pipe: raw (unbuffered) ones may take part of it per write
                view = memoryview(data)
                while view:
                    written = self._out.write(view)
                    if written is None:
                        continue
                    view = view[written:] if isinstance(written, int) and written < len(view) else view[len(view):]
                flush = getattr(self._out, "flush", None)
                if flush is not None:
                    flush()

    def close(self) -> None:
        with self._close_lock:
            if self.closed:
                return
            self.closed = True
        for end in {id(self._in): self._in, id(self._out): self._out}.values():
            shutdown = getattr(end, "shutdown", None)
            if shutdown is not None:            # wakes a reader blocked in recv on another thread
                try:
                    shutdown(2)                 # socket.SHUT_RDWR
                except (OSError, ValueError):
                    pass
            try:
                end.close()
            except (OSError, ValueError):
                pass
