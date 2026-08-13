#!/usr/bin/env python3
"""
Run CopyShop from a checkout: `python3 main.py [datei.pdf]`.

The application itself is tools/app.py. This is here because the README has
always documented running the repository this way, and because a top-level
module named "main" must not be what gets installed — see the note there.
"""
from tools.app import main

if __name__ == "__main__":
    main()
