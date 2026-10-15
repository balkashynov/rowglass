"""Explicit libpq COPY BOTH transport with no automatic WAL acknowledgment.

psycopg2 handles connection establishment, authentication and START_REPLICATION.
Its read_message() can advance flush_lsn over keepalives, so the recording loop
uses libpq directly through the same loaded extension and PGconn pointer. Only
the caller's durable commit position is sent in replication feedback.
"""

from __future__ import annotations
import ctypes
import datetime as dt
import select
import struct
import time
import psycopg2._psycopg


class CopyBoth:
    def __init__(self, connection):
        self.connection = connection
        self.lib = ctypes.CDLL(psycopg2._psycopg.__file__)
        self.pointer = ctypes.c_void_p(connection.pgconn_ptr)
        for name, result, args in [
            ("PQconsumeInput", ctypes.c_int, [ctypes.c_void_p]),
            (
                "PQgetCopyData",
                ctypes.c_int,
                [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_int],
            ),
            ("PQfreemem", None, [ctypes.c_void_p]),
            (
                "PQputCopyData",
                ctypes.c_int,
                [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int],
            ),
            ("PQflush", ctypes.c_int, [ctypes.c_void_p]),
            ("PQsetnonblocking", ctypes.c_int, [ctypes.c_void_p, ctypes.c_int]),
            ("PQerrorMessage", ctypes.c_char_p, [ctypes.c_void_p]),
        ]:
            function = getattr(self.lib, name)
            function.restype = result
            function.argtypes = args
        if self.lib.PQsetnonblocking(self.pointer, 1) != 0:
            self.fail()
        self.last_ack = 0

    def fail(self):
        raise RuntimeError(
            "Replication transport: "
            + self.lib.PQerrorMessage(self.pointer).decode(errors="replace")
        )

    def read(self):
        if self.lib.PQconsumeInput(self.pointer) != 1:
            self.fail()
        buffer = ctypes.c_void_p()
        size = self.lib.PQgetCopyData(self.pointer, ctypes.byref(buffer), 1)
        if size == 0:
            return None
        if size < 0:
            self.fail()
        try:
            if size > 128 * 1024 * 1024:
                raise ValueError("Replication envelope exceeds 128 MiB")
            data = ctypes.string_at(buffer, size)
        finally:
            self.lib.PQfreemem(buffer)
        return self.decode(data)

    @staticmethod
    def decode(data):
        if data[:1] == b"w" and len(data) >= 25:
            start, end, sent = struct.unpack("!QQq", data[1:25])
            return {
                "kind": "data",
                "start": start,
                "end": end,
                "sent": sent,
                "payload": data[25:],
            }
        if data[:1] == b"k" and len(data) == 18:
            end, sent, reply = struct.unpack("!QqB", data[1:])
            return {"kind": "keepalive", "end": end, "sent": sent, "reply": bool(reply)}
        raise ValueError("Invalid PostgreSQL replication envelope")

    def feedback(self, ack):
        if ack < self.last_ack:
            raise ValueError("Replication ACK cannot move backward")
        now = (
            dt.datetime.now(dt.timezone.utc)
            - dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
        ) // dt.timedelta(microseconds=1)
        payload = b"r" + struct.pack("!QQQqB", ack, ack, ack, now, 0)
        deadline = time.monotonic() + 10
        while True:
            status = self.lib.PQputCopyData(self.pointer, payload, len(payload))
            if status == 1:
                break
            if status < 0:
                self.fail()
            if time.monotonic() > deadline:
                raise TimeoutError("Replication feedback send timed out")
            self.lib.PQflush(self.pointer)
            select.select([], [self.connection], [], 0.1)
        while True:
            status = self.lib.PQflush(self.pointer)
            if status == 0:
                break
            if status < 0:
                self.fail()
            if time.monotonic() > deadline:
                raise TimeoutError("Replication feedback flush timed out")
            select.select([], [self.connection], [], 0.1)
        self.last_ack = ack
