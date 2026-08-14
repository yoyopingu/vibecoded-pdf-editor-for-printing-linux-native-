#!/usr/bin/env python3
"""
Run CopyShop from a checkout: `python3 main.py [datei.pdf]`.

The application itself is tools/app.py. This is here because the README has
always documented running the repository this way, and because a top-level
module named "main" must not be what gets installed — see the note there.

The import sits inside the guard on purpose. The pre-render pool starts its
workers with the "spawn" method, and a spawned child re-imports whatever the
parent's __main__ was — this file. At module level, that import pulled Qt, all
thirteen tool panels and the logging setup into every worker, for a function
none of them call.
"""

if __name__ == "__main__":
    from tools.app import main
    main()
