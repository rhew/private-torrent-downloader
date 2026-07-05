from __future__ import annotations

import sys

from .app import main


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"curator: {error}", file=sys.stderr)
        sys.exit(1)
