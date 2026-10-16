"""Entry point for the Fatcopy window application."""

import multiprocessing
import sys

from fatcopy.gui import main

if __name__ == "__main__":
    # Required so a frozen executable never re-runs the whole program when a
    # library spawns a helper process.
    multiprocessing.freeze_support()
    sys.exit(main(sys.argv[1:]))
