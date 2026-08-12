#!/usr/bin/env python3
"""
CopyShop regression suite — runs without pytest.

    python3 tests/run.py               everything
    python3 tests/run.py zoom render    only modules whose name contains one of these

pytest works too, and is the nicer way to run one test or read a failure:

    pytest tests/ -x -k thumbnail

Both are kept. The system interpreter this app runs on has PyQt6, pypdfium2,
pikepdf, reportlab and img2pdf but no pytest, so a suite that needed pytest
would be a suite the app's own machine could not run.
"""
import importlib
import os
import pathlib
import shutil
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import support        # noqa: E402  — bootstraps Qt and the fixtures

HERE = pathlib.Path(__file__).parent


def modules(patterns):
    for f in sorted(HERE.glob("test_*.py")):
        if not patterns or any(p in f.stem for p in patterns):
            yield importlib.import_module(f"tests.{f.stem}")


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
    main(sys.argv[1:])
