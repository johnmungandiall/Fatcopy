"""Entry point for the Fatcopy command line tool."""

import multiprocessing
import sys

from fatcopy.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main(sys.argv[1:]))
