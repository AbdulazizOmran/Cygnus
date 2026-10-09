"""Every backend.X(...) call in the QML pages matches the arity of its @Slot in bridge.py. A mismatch is a runtime
error that no unit test of the Python side would ever see (the smoke test does not click every button)."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "cygnus/gui"


def _slots() -> dict[str, int]:
    """name -> number of arguments after `self`, for every method decorated with @Slot."""
    tree = ast.parse((ROOT / "bridge.py").read_text())
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and getattr(dec.func, "id", "") == "Slot":
                    out[node.name] = len(node.args.args) - 1
                    declared = [a for a in dec.args if not isinstance(a, ast.keyword)]
                    assert len(declared) == out[node.name], f"{node.name}: @Slot types and arguments disagree"
    return out


def _calls(text: str):
    """(name, argument count, line) for every `backend.name(...)`: top-level commas counted, nesting respected."""
    for m in re.finditer(r"\bbackend\.([A-Za-z_]\w*)\(", text):
        i, depth, args, seen = m.end(), 1, 0, False
        quote = None
        while i < len(text) and depth:
            c = text[i]
            if quote:
                if c == "\\":
                    i += 1
                elif c == quote:
                    quote = None
            elif c in "'\"`":
                quote = c
            elif c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            elif c == "," and depth == 1:
                args += 1
            elif not c.isspace() and depth == 1:
                seen = True
            i += 1
        yield m.group(1), (args + 1 if seen else 0), text.count("\n", 0, m.start()) + 1


def test_every_backend_call_in_qml_matches_its_slot():
    slots = _slots()
    problems = []
    called = set()
    for qml in sorted((ROOT / "qml").glob("*.qml")):
        for name, count, line in _calls(qml.read_text()):
            called.add(name)
            if name not in slots:
                if name not in ("refreshApps", "refreshStorage"):  # also callable as plain methods of the object
                    problems.append(f"{qml.name}:{line} backend.{name}() is not a @Slot")
            elif slots[name] != count:
                problems.append(f"{qml.name}:{line} backend.{name}() is called with {count} argument(s); "
                                f"the slot takes {slots[name]}")
    assert problems == []
    assert len(called) > 25  # the scan really found the calls


def test_the_scan_itself_counts_correctly():
    calls = list(_calls('backend.a(); backend.b(x, y); backend.c(f(1, 2), "a, b", [1, 2]); backend.d(\n  p,\n  q)'))
    assert [(n, c) for n, c, _ in calls] == [("a", 0), ("b", 2), ("c", 3), ("d", 2)]
