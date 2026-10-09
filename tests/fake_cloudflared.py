"""A stand-in for cloudflared: prints what a quick tunnel prints, then runs until it is stopped.

FAKE_TUNNEL=fail   exit at once with an error, like cloudflared without internet
FAKE_TUNNEL=drop   connect, then exit after a moment (the link dropped)
FAKE_TUNNEL_NAME   the address's name (default: seasonal-deck-organisms-sf)
"""

import os
import sys
import time

mode = os.environ.get("FAKE_TUNNEL", "")
name = os.environ.get("FAKE_TUNNEL_NAME", "seasonal-deck-organisms-sf")
err = sys.stderr
print("2026-10-09T14:45:24Z INF Requesting new quick Tunnel on trycloudflare.com...", file=err, flush=True)
if mode == "fail":
    print('failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": dial tcp: no such host', file=err, flush=True)
    sys.exit(1)
print("2026-10-09T14:45:26Z INF |  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |",
      file=err, flush=True)
print(f"2026-10-09T14:45:26Z INF |  https://{name}.trycloudflare.com                                     |", file=err, flush=True)
print(f"2026-10-09T14:45:26Z INF args: {' '.join(sys.argv[1:])}", file=err, flush=True)
print("2026-10-09T14:45:27Z INF Registered tunnel connection connIndex=0 location=ord08 protocol=quic", file=err, flush=True)
if mode == "drop":
    time.sleep(0.5)
    sys.exit(1)
while True:
    time.sleep(1)
