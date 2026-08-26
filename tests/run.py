#!/usr/bin/env python3
"""
CopyShop regression suite — runs without pytest.

    python3 tests/run.py               everything, one process per module, in parallel
    python3 tests/run.py quick         the fast subset (~45 s), one process per module
    python3 tests/run.py zoom render   only these, in this process
    python3 tests/run.py --one-process everything in a single process

Full runs execute one process per module, launched in parallel: the number of
workers = min(cpu count, module count) (override with FOLIO_TEST_WORKERS for
memory-bound machines), and each module still runs in its own child process
for heap-fault isolation. --one-process forces the serial in-process path through main(), the
reproduction path for the fault.

Both are kept. The system interpreter this app runs on has PyQt6, pypdfium2,
pikepdf, reportlab and img2pdf but no pytest, so a suite that needed pytest
would be a suite the app's own machine could not run.

Why a process per module
-------------------
A full run in one process dies of a heap fault roughly three times in eight,
deep in, after most of the tests have already reported PASS. Every test passes;
a crashed run is one that got most of the way and fell over. A single module has
never been seen to crash, under either runner — so a full pass is ten short
processes rather than one long one, and it is reliable.

That is a way around the fault, not a fix for it. --one-process is kept so the
next person to look at it has the reproduction to hand.
"""
import importlib
import os
import pathlib
import shutil
import sys
import traceback
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import support        # noqa: E402  — bootstraps Qt and the fixtures

HERE = pathlib.Path(__file__).parent


# The fast subset for the dev loop: every layer a small change can reach,
# none of the Ghostscript / CUPS / subprocess-spawning heavies. Measured
# ~45 s against ~3 m 40 s for the full run. The full run stays the answer
# before calling anything done — see AGENTS.md.
PRESETS = {
    "quick": ["test_hygiene", "test_cancel", "test_render", "test_manage",
              "test_viewer_zoom", "test_rulers", "test_app", "test_empty_state"],
}


def modules(patterns):
    for f in sorted(HERE.glob("test_*.py")):
        if not patterns or any(p in f.stem for p in patterns):
            yield importlib.import_module(f"tests.{f.stem}")


def _run_module(stem):
    """One child process per module. Returns the CompletedProcess."""
    import subprocess
    return subprocess.run([sys.executable, "-u", __file__, stem],
                          capture_output=True, text=True)


def isolated(mods=None):
    """One child process per module, launched in parallel. Returns the exit code."""
    if mods is None:
        mods = sorted(f.stem for f in HERE.glob("test_*.py"))
    if not mods:
        print("\n0 passed, 0 failed")
        return 0
    workers = max(1, min(os.cpu_count() or 1, len(mods)))
    env_workers = os.environ.get("FOLIO_TEST_WORKERS")
    if env_workers and env_workers.isdigit() and int(env_workers) > 0:
        workers = min(int(env_workers), len(mods))
    passed = failed = crashed = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for stem, r in zip(mods, ex.map(_run_module, mods)):
            sys.stdout.write(r.stdout)
            for line in r.stdout.splitlines():
                if line.strip().endswith("failed") and " passed, " in line:
                    p, f = line.split(" passed, ")
                    passed += int(p.strip()); failed += int(f.split()[0])
            if r.returncode != 0:
                crashed += 1
                sys.stdout.write(f"  !! {stem} exited {r.returncode}\n{r.stderr[-800:]}\n")
    print(f"\n{passed} passed, {failed} failed"
          + (f", {crashed} module(s) crashed" if crashed else ""))
    return 0 if not (failed or crashed) else 1


def main(patterns):
    passed = failed = 0
    for mod in modules(patterns):
        tests = [v for k, v in sorted(vars(mod).items())
                 if k.startswith("test_") and callable(v)
                 and getattr(v, "__module__", None) == mod.__name__]
        if not tests:
            continue
        print(f"\n{mod.__name__.split('.')[-1]}")
        for t in tests:
            try:
                note = t()
                extra = f"  ({note})" if isinstance(note, str) else ""
                print(f"  PASS  {t.__name__}{extra}")
                passed += 1
            except Exception as e:
                print(f"  FAIL  {t.__name__}: {e}\n{traceback.format_exc()}")
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    shutil.rmtree(support._TMP, ignore_errors=True)
    sys.stdout.flush()
    # Skip Qt / daemon-thread teardown, which segfaults harmlessly on the way
    # out and would turn a green run into a non-zero exit.
    os._exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    raw = sys.argv[1:]
    args = [a for a in raw if a != "--one-process"]
    # A preset expands to its modules; several modules then run one process
    # each, the same way the full run does, rather than through main().
    stems = list(dict.fromkeys(s for a in args for s in PRESETS.get(a, [a])))
    if "--one-process" in raw:
        main(stems)
    elif not args:
        raise SystemExit(isolated())
    elif stems != args:
        raise SystemExit(isolated(stems))
    else:
        main(stems)
