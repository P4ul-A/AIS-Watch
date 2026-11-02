"""Run one HTTP server and one serial collector; no development reloader."""

import fcntl
import logging
import os
import signal

from waitress import create_server

from .storage import Store
from .web import create_app
from .worker import Worker


def main():
    """Hold an archive lock, start services, and checkpoint on SIGTERM/SIGINT."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = Store()
    store.directory.mkdir(parents=True, exist_ok=True)
    with (store.directory / "worker.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another AIS-Watch process already owns this archive.") from None
        store.initialize()
        app = create_app(store)
        worker = Worker(store)
        app.extensions["worker"] = worker
        server = create_server(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9101")), threads=4)

        def shutdown(signum, frame):
            worker.stop.set()
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        worker.start()
        try:
            server.run()
        except KeyboardInterrupt:
            pass
        finally:
            worker.stop.set()
            server.close()
            worker.thread.join(timeout=25)


if __name__ == "__main__":
    main()
