"""Wait for the real local lab endpoints, without creating or modifying data."""

import time
import urllib.request
import psycopg2

endpoints = [
    "host=127.0.0.1 port=55432 user=rowglass password=rowglass-local dbname=rowglass",
    "host=127.0.0.1 port=59900 user=peerdb password=rowglass-local dbname=peerdb",
]
deadline = time.monotonic() + 180
last = None
while time.monotonic() < deadline:
    try:
        for dsn in endpoints:
            with psycopg2.connect(dsn, connect_timeout=3) as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
                assert cur.fetchone()[0] == 1
        with urllib.request.urlopen(
            "http://127.0.0.1:58123/ping", timeout=3
        ) as response:
            assert response.read().strip() == b"Ok."
        print(
            "Rowglass database endpoints are ready. Bootstrap can now create the mirror."
        )
        break
    except (psycopg2.Error, OSError, AssertionError) as error:
        last = error
        time.sleep(2)
else:
    raise SystemExit("Lab not ready after 180 seconds: " + str(last))
