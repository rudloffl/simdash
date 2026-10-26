"""simdash - AMS2 live telemetry dashboard and session recorder.

    simdash [--host 127.0.0.1] [--port 8050]

Only one instance may record at a time (lock file in the data dir).
"""

import argparse
import fcntl
import logging
import sys

import uvicorn

from . import store
from .app import create_app


def main():
    ap = argparse.ArgumentParser(prog="simdash", description=__doc__.split("\n")[0])
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to open it from a phone/tablet on the LAN")
    ap.add_argument("--port", type=int, default=8050)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    store.DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(store.DATA_DIR / "simdash.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("simdash is already running (another terminal, or: systemctl --user status simdash). "
                 "Open that one in the browser, default http://127.0.0.1:8050")

    logging.getLogger("simdash").info("data in %s, open http://%s:%d", store.DATA_DIR, args.host, args.port)
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
