#!/usr/bin/env python3
"""
CopyShop regression suite — runs without pytest.

    python3 tests/run.py               everything, one process per module
    python3 tests/run.py zoom render   modules matching either name
    python3 tests/run.py pdfx::test_images_come_down_to_press_resolution
    python3 tests/run.py --timings      per-test and per-module timings
    python3 tests/run.py --one-process everything in a single process

pytest works too, and is the nicer way to run one test or read a failure:

    pytest tests/ -x -k thumbnail

Both are kept. The system interpreter this app runs on has PyQt6, pypdfium2,
pikepdf, reportlab and img2pdf but no pytest, so a suite that needed pytest
would be a suite the app's own machine could not run.

Why a process per module
------------------------
A full run in one process dies of a heap fault roughly three times in eight,
deep in, after most of the tests have already reported PASS. The printing
checks also faulted when many dialogs and pdfium renders shared one module;
they now run in smaller behavior modules. A full pass uses one child process
per module so native state does not carry across subjects.

That is a way around the fault, not a fix for it. --one-process is kept so the
next person to look at it has the reproduction to hand.
"""
import importlib
import os
import pathlib
import shutil
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HERE = pathlib.Path(__file__).parent


def _selection(patterns):
    """Resolve a module filter or an exact module::test selector before Qt loads."""
    stems = sorted(f.stem for f in HERE.glob("test_*.py"))
    if not patterns:
        return {stem: None for stem in stems}
    chosen = {}
    for pattern in patterns:
        if "::" in pattern:
            module, test = pattern.split("::", 1)
            stem = module if module.startswith("test_") else f"test_{module}"
            if stem not in stems or not test.startswith("test_"):
                raise ValueError(f"unknown test selector: {pattern}")
            if stem not in chosen:
                chosen[stem] = set()
            if chosen[stem] is not None:
                chosen[stem].add(test)
        else:
            matches = [pattern] if pattern in stems else [
                stem for stem in stems if pattern in stem]
            if not matches:
                raise ValueError(f"no test module matches: {pattern}")
            for stem in matches:
                chosen[stem] = None
    return chosen


def isolated(selection, timings=False):
    """One child process per module. Returns the exit code."""
    import subprocess
    passed = failed = crashed = 0
    for stem, names in selection.items():
        args = [stem] if names is None else [f"{stem}::{name}" for name in sorted(names)]
        start = time.perf_counter()
        r = subprocess.run([sys.executable, "-u", __file__, "--one-process",
                            *(["--timings"] if timings else []), *args],
                           capture_output=True, text=True)
        elapsed = time.perf_counter() - start
        sys.stdout.write(r.stdout)
        module_failed = 0
        for line in r.stdout.splitlines():
            if line.strip().endswith("failed") and " passed, " in line:
                p, f = line.split(" passed, ")
                passed += int(p.strip())
                module_failed = int(f.split()[0])
                failed += module_failed
        # An ordinary assertion failure exits 1 after its summary. A signal or
        # a child that dies before the summary is a separate native crash.
        if r.returncode != 0 and module_failed == 0:
            crashed += 1
            sys.stdout.write(f"  !! {stem} exited {r.returncode}\n{r.stderr[-800:]}\n")
        if timings:
            print(f"  MODULE {stem}: {elapsed:.3f}s wall")
        sys.stdout.flush()
    print(f"\n{passed} passed, {failed} failed"
          + (f", {crashed} module(s) crashed" if crashed else ""))
    return 0 if not (failed or crashed) else 1


def main(selection, timings=False):
    # Importing support starts QApplication and builds fixtures. A single-test
    # run should only pay that cost in the child that actually runs the test.
    from tests import support
    passed = failed = 0
    for stem, selected_names in selection.items():
        import_start = time.perf_counter()
        mod = importlib.import_module(f"tests.{stem}")
        tests = [v for k, v in sorted(vars(mod).items())
                 if k.startswith("test_") and callable(v)
                 and getattr(v, "__module__", None) == mod.__name__
                 and (selected_names is None or k in selected_names)]
        if selected_names is not None:
            missing = selected_names - {t.__name__ for t in tests}
            if missing:
                print(f"unknown test in {stem}: {', '.join(sorted(missing))}")
                failed += len(missing)
        if not tests:
            continue
        print(f"\n{stem}")
        for t in tests:
            start = time.perf_counter()
            try:
                note = t()
                extra = f"  ({note})" if isinstance(note, str) else ""
                duration = f" [{time.perf_counter() - start:.3f}s]" if timings else ""
                print(f"  PASS  {t.__name__}{duration}{extra}")
                passed += 1
            except Exception as e:
                duration = f" [{time.perf_counter() - start:.3f}s]" if timings else ""
                print(f"  FAIL  {t.__name__}{duration}: {e}\n{traceback.format_exc()}")
                failed += 1
        if timings:
            print(f"  TESTS {stem}: {time.perf_counter() - import_start:.3f}s including import")
    print(f"\n{passed} passed, {failed} failed")
    shutil.rmtree(support._TMP, ignore_errors=True)
    sys.stdout.flush()
    # Skip Qt / daemon-thread teardown, which segfaults harmlessly on the way
    # out and would turn a green run into a non-zero exit.
    os._exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    args = sys.argv[1:]
    try:
        selection = _selection([a for a in args if not a.startswith("--")])
        unknown = [a for a in args if a.startswith("--") and a not in
                   ("--one-process", "--timings")]
        if unknown:
            raise ValueError(f"unknown option: {', '.join(unknown)}")
    except ValueError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(2)
    if "--one-process" not in args:
        raise SystemExit(isolated(selection, "--timings" in args))
    main(selection, "--timings" in args)
