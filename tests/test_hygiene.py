"""
Hygiene: things about the source that no runtime test would reach.

These exist because splitting the big modules moved imports around, and an
import that ended up in the wrong module only fails on the line that needs it —
which may be an error path, a printer fallback, or a paint routine that runs
once a session. Two such NameErrors sat in tools/printing for exactly that
reason: preview.py called QTimer.singleShot in showEvent without importing
QTimer, and dialog.py called pil_to_qpixmap while preview.py was the module
that imported it.
"""
import ast
import builtins
import pathlib
import symtable

REPO = pathlib.Path(__file__).resolve().parent.parent
TOOLS = REPO / "tools"
_BUILTINS = set(dir(builtins))


def _sources():
    return sorted(TOOLS.rglob("*.py"))


def test_every_global_name_has_a_module_level_binding():
    """A name a function reads out of module scope has to be bound there.

    symtable settles what the eye cannot: for every scope it reports whether a
    name is local, a parameter, imported, assigned or free. Anything left over
    is read from the module's own namespace, so the module must define it —
    otherwise the line raises NameError the first time it runs."""
    missing = []
    for f in _sources():
        st = symtable.symtable(f.read_text(), str(f), "exec")
        module_names = {s.get_name() for s in st.get_symbols()}

        def walk(tab, path):
            for sym in tab.get_symbols():
                name = sym.get_name()
                if not sym.is_referenced():
                    continue
                if (sym.is_local() or sym.is_parameter() or sym.is_imported()
                        or sym.is_free() or sym.is_assigned()):
                    continue
                if name in module_names or name in _BUILTINS or name.startswith("__"):
                    continue
                missing.append(f"{f.relative_to(REPO)}: {'.'.join(path)} reads {name}")
            for child in tab.get_children():
                walk(child, path + (child.get_name(),))

        walk(st, ())
    assert not missing, "names read from module scope but never bound there:\n  " + \
                        "\n  ".join(missing)


def test_no_import_is_left_unused():
    """An import nothing references is the other half of the same mistake: the
    name landed in a module that does not need it, and usually the module that
    does went without."""
    unused = []
    for f in _sources():
        if f.name == "__init__.py":
            continue          # re-export surface: bound on purpose, used elsewhere
        src = f.read_text()
        lines = src.splitlines()
        tree = ast.parse(src)
        bound = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    if a.name != "*":
                        bound.append((a.asname or a.name.split(".")[0], node.lineno))
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute):
                root = n
                while isinstance(root, ast.Attribute):
                    root = root.value
                if isinstance(root, ast.Name):
                    used.add(root.id)
        for name, line in sorted(set(bound), key=lambda kv: kv[1]):
            if name in used or "noqa" in lines[line - 1]:
                continue
            unused.append(f"{f.relative_to(REPO)}:{line} imports {name}")
    assert not unused, "imports nothing uses:\n  " + "\n  ".join(unused)


def test_no_local_import_repeats_the_module_s_own():
    """Re-importing at the top of a function what the module already imported
    unconditionally does nothing but hide where the name comes from."""
    dupes = []
    for f in _sources():
        tree = ast.parse(f.read_text())
        top = {}
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    top[a.asname or a.name.split(".")[0]] = (
                        getattr(node, "module", None), a.name)
        seen = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)) or node in tree.body:
                continue
            if id(node) in seen:
                continue            # ast.walk reaches nested functions twice
            seen.add(id(node))
            mod = getattr(node, "module", None)
            for a in node.names:
                name = a.asname or a.name.split(".")[0]
                if top.get(name) == (mod, a.name):
                    dupes.append(f"{f.relative_to(REPO)}:{node.lineno} re-imports {name}")
    assert not dupes, "local imports the module already has:\n  " + "\n  ".join(dupes)
