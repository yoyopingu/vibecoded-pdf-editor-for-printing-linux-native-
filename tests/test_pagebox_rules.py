"""The two rules the page manager's view depends on.

They are rules about the source, not about a runtime: nothing an end user
does would show that a second place had started writing the view on its own.
Both rules exist because each of them was broken once.

The first: the pages of the view are written in one place. A page copied in
the page manager is a second entry for the *same* source page, and the turn
belongs on the writer's copy of it. Ten places wrote that view, and eight of
them wrote it wrong — the second copy of a turned page came out a further 90
degrees round than the first.

The second: a quarter turn is told apart from a half turn in one place. The
test is ``rotation % 180``, and a page turned by 180 degrees keeps its shape
while a quarter turn swaps it. Written out in twelve places, it was one
mistake away from turning a portrait sheet sideways in eleven of them.
"""
import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
TOOLS = REPO / "tools"

# Writing a page into a pypdf writer: add_page, the helper that turns it, and
# writer.append (which is told apart from list.append by its arguments).
_WRITERS = ("add_page", "add_turned_page")


def _sources():
    return sorted(TOOLS.rglob("*.py"))


def _calls(node):
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)]


def _name(call):
    fn = call.func
    if isinstance(fn, ast.Attribute):
        return fn.attr
    if isinstance(fn, ast.Name):
        return fn.id
    return None


def _writes_a_page(call):
    """True when the call puts a page into a pypdf writer."""
    name = _name(call)
    if name in _WRITERS:
        return True
    if name == "append" and isinstance(call.func, ast.Attribute):
        # writer.append(reader, pages=[...]) — a list.append has one argument
        # and no pages keyword.
        return len(call.args) >= 2 or any(k.arg == "pages" for k in call.keywords)
    return False


def _resolves_a_turn(call):
    return _name(call) == "get_rotation"


# The print path writes the subset itself: it rebuilds the form for the
# printer (tools/printing/content.py) instead of keeping the catalog's
# /AcroForm, and that is the code the shop is billed for.
_ALLOWED_WRITERS = {
    ("tools/printing/spool.py", "write_subset_pdf"):
        "the print path rebuilds the form for the printer",
}


def test_the_view_is_written_in_one_place():
    """A place that turns a page must not also write it.

    The turn belongs on the writer's copy of the page: pypdf's rotate() adds
    to the /Rotate it finds, and a reader hands out the same page object for
    every copy of a source page. Turning the reader's page for the second
    copy turned it once more than the first. Writing the view is therefore
    the job of tools/pagebox.py alone."""
    offenders = []
    for f in _sources():
        tree = ast.parse(f.read_text())
        stack = []

        def check(node):
            calls = _calls(node)
            if not any(_resolves_a_turn(c) for c in calls):
                return
            if not any(_writes_a_page(c) for c in calls):
                return
            key = (str(f.relative_to(REPO)), stack[-1] if stack else "<module>")
            reason = _ALLOWED_WRITERS.get(key)
            if reason is None:
                offenders.append(f"{key[0]}:{key[1]}")

        class V(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                stack.append(node.name); check(node)
                self.generic_visit(node); stack.pop()

            def visit_AsyncFunctionDef(self, node):
                stack.append(node.name); check(node)
                self.generic_visit(node); stack.pop()

        V().visit(tree)
    assert not offenders, (
        "a place that turns a page writes it as well — the turn belongs on "
        "the writer's copy:\n  " + "\n  ".join(offenders))


# The places that still spell the quarter turn out. Each one is a spot that
# has to be watched; the list may only get shorter, and a new one is a place
# that has to be looked at before it is added.
_QUARTER_TURN_SITES = {
    ("tools/panels/crop_resize.py", "ready"): 1,
    ("tools/panels/crop_resize.py", "_known_preview_size"): 1,
    ("tools/panels/crop_resize.py", "_render_preview"): 2,
    ("tools/panels/impose.py", "_page_image"): 1,
    ("tools/panels/impose.py", "_booklet_page_sizes"): 1,
    ("tools/printing/handling.py", "_visual_size"): 1,
    ("tools/printing/spool.py", "oriented_sheet_pt"): 1,
    ("tools/render/queue.py", "run"): 1,
    ("tools/render/region.py", "page_px_size"): 1,
    ("tools/viewer/single_page.py", "_known_page_size"): 1,
    ("tools/viewer/single_page.py", "_ensure_page_dims"): 2,
}


def test_a_quarter_turn_is_told_apart_from_a_half_turn_in_one_place():
    """``rotation % 180`` is the test, and it belongs to pagebox.

    A turn of 90 or 270 degrees swaps the page's width and height, a turn of
    180 degrees does not. The predicate is written out in a dozen places,
    each of them a place where a landscape sheet can end up on a portrait
    one. The list below is the worklist: it may only get shorter."""
    found = {}
    for f in _sources():
        if f == TOOLS / "pagebox.py":
            continue                      # the predicate lives here
        tree = ast.parse(f.read_text())
        stack = []

        class V(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                stack.append(node.name); self.generic_visit(node); stack.pop()

            def visit_AsyncFunctionDef(self, node):
                stack.append(node.name); self.generic_visit(node); stack.pop()

            def visit_BinOp(self, node):
                if (isinstance(node.op, ast.Mod) and isinstance(node.right, ast.Constant)
                        and node.right.value == 180):
                    key = (str(f.relative_to(REPO)),
                           stack[-1] if stack else "<module>")
                    found[key] = found.get(key, 0) + 1
                self.generic_visit(node)

        V().visit(tree)

    new = {k: n for k, n in found.items() if _QUARTER_TURN_SITES.get(k) != n}
    assert not new, (
        "the quarter turn is spelled out somewhere it is not watched for:\n  "
        + "\n  ".join(f"{k[0]}:{k[1]} ({n})" for k, n in sorted(new.items())))
    assert set(found) == set(_QUARTER_TURN_SITES), (
        "the list of quarter-turn sites no longer matches the code:\n  "
        + "\n  ".join(f"{k[0]}:{k[1]}" for k in sorted(set(found) ^ set(_QUARTER_TURN_SITES))))
