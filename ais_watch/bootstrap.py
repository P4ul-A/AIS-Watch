"""Prepare private storage before dropping container privileges."""

import os
import sys

from .storage import Store


def main():
    """Prepare storage as root; run the actual service as UID 1000."""
    store = Store()
    store.directory.mkdir(parents=True, exist_ok=True)
    store.initialize_credentials()
    if os.geteuid() == 0:
        os.chown(store.directory, 1000, 1000)
        os.chown(store.directory / "credentials.json", 1000, 1000)
        os.setgroups([])
        os.setgid(1000)
        os.setuid(1000)
    os.execv(sys.executable, [sys.executable, "-m", "ais_watch"])


if __name__ == "__main__":
    main()
