"""Do connections that send nothing stop Django from answering real requests?

Opens idle connections straight to Gunicorn, then times one normal request. Exits 1 if it took over LIMIT seconds.
Usage: python scripts/idle_connections_probe.py [idle connections]
"""

import http.client
import socket
import sys
import time

HOST, PORT, LIMIT = "127.0.0.1", 8001, 1.0
CRLF = bytes([13, 10])
REQUEST = CRLF.join([b"GET /api/v1/events/LOADTEST/availability HTTP/1.1", b"Host: localhost", b"", b""])


def main() -> None:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 64
    idle = [socket.create_connection((HOST, PORT), timeout=5) for _ in range(count)]
    time.sleep(0.2)
    started = time.perf_counter()
    connection = http.client.HTTPConnection(HOST, PORT, timeout=120)
    connection.request("GET", "/api/v1/events/LOADTEST/availability", headers={"Host": "localhost"})
    status = connection.getresponse().status
    took = time.perf_counter() - started
    for sock in idle:
        sock.close()
    print(f"{count} idle connections: reply {status} in {took:.2f} s")
    if status != 200 or took > LIMIT:
        raise SystemExit(f"Too slow: the limit is {LIMIT} s")

    # A connection that waits before its request must still be answered.
    late = socket.create_connection((HOST, PORT), timeout=5)
    time.sleep(0.2)
    late.sendall(REQUEST)
    reply = late.recv(64).split(CRLF)[0]
    late.close()
    print("request sent after 0.2 s:", reply.decode())
    if b" 200 " not in reply:
        raise SystemExit("A late request was not answered")


if __name__ == "__main__":
    main()
