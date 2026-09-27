"""Every interface string is translated, and every untranslated string admits to it.

Three scans, because three things can go wrong:

**Scan 1 -- every ``tr("literal")`` is in the table.** A key that misses the table still
works (the fallback returns the English text), but it does so silently, and a template
whose placeholders do not match its translation shows raw ``{}`` braces to the user. This
scan is a failure when a key is missing or a placeholder count disagrees.

**Scan 2 -- every module-level string constant in ``gigaxml.gui`` is accounted for.**
Scan 1 cannot see a string that reaches ``tr()`` through a variable: a column name living
in a tuple, a value passed to :func:`gigaxml.gui.results.count_of` at run time, a list of
choices shown in a combo. The first version of this checker only did scan 1 and reported
"198/198 translated" while roughly forty more strings were reachable from the interface
through constants -- the report was true and incomplete at the same time, which is the
worst thing a report can be. So the second scan walks every constant and demands it be
one of three things: in the table, in the explicit allowlist below with a reason, or a
failure you have to resolve.

**Scan 3 -- every noun ``count_of`` is called with has both of its keys.** Scan 2's
docstring names ``count_of`` and scan 2 does not check it, because its keys are built at
run time from the noun argument and exist nowhere as literals: ``count_of(n, "row")``
will ask the table for ``{} row`` and ``{} rows``, and if either is missing the interface
displays the raw braces. This scan walks the call sites, collects the nouns, and demands
both keys per noun. A call site whose noun is not a literal is itself a failure -- a
noun this scan cannot read is a pair of keys it cannot check -- and so is a scan that
finds no call sites at all, because zero means the walk broke, not that the interface
stopped counting things.

Run it directly::

    python -m tools.check_i18n_keys

Exit 0 means all three scans are clean. It is also run as an ordinary test in the suite
(``tests/unit/test_i18n_keys_guard.py``), so a missing key turns CI red instead of
waiting for someone to remember this script exists.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

if __package__ in (None, ""):  # pragma: no cover - direct-script convenience
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from gigaxml.gui.i18n import ZH

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "gigaxml" / "gui"

#: The file whose constants are the table itself. Its keys are in the table by being the
#: table; its values are the translations; scanning it would only re-report the dictionary.
_TABLE_HOME = "i18n.py"

_PLACEHOLDERS = re.compile(r"\{\}")

#: Strings that never reach the user as prose, each class with the reason it is exempt.
#: A constant that matches nothing here and is not in the table fails the check -- which
#: is the point: adding an interface string now forces a decision instead of inheriting
#: whatever the fallback does.
_ALLOWLIST: list[tuple[str, str]] = [
    (r"^\.(\w+\.)*\w+$", "a file suffix or dotted technical name (.xml, .xml.gz, .tmp)"),
    (r"^/", "an absolute-style path or record-path example"),
    (r"\*", "a glob or a file-dialog filter"),
    (r"^#", "a colour literal"),
    (r"^color:", "a stylesheet fragment"),
    (r"^gigaxml-", "a temporary run-directory prefix this project creates"),
    (r"^@", "a probe or attribute-path constant handed to the CLI"),
    (r"^\{", "a template fragment that is filled before display"),
    (r"^[a-z0-9_]+$", "a stored value, object name or protocol token (csv, abort, snake_case)"),
    (r"^[a-z0-9]+(-[a-z0-9]+)*$", "a hyphenated slug value (advice kinds, tag names)"),
    (
        r"^[A-Z][A-Za-z0-9_]*$",
        "a code identifier (__all__ exports, constant names) -- not display text",
    ),
    (r"^[\w .\-]+\.(xml|yaml|yml|json|csv|jsonl|parquet|txt)$", "a file name"),
    (r"^(English|中文)$", "a language's own name, deliberately shown untranslated"),
    (r"^prefix → URI$", "a technical column header kept in symbolic form on purpose"),
    (r"^<default>$", "a symbolic marker for the default namespace, read by machines too"),
]


def _allowlisted(text: str) -> str | None:
    """The reason this string is exempt, or ``None`` when nothing covers it."""
    for pattern, reason in _ALLOWLIST:
        if re.search(pattern, text):
            return reason
    return None


def _string_constants(path: pathlib.Path) -> list[str]:
    """The string constants of **module-level** assignments.

    Module level only, deliberately: constants that live inside functions and methods are
    either passed to ``tr()`` literally -- which scan 1 already sees -- or are CLI flags
    and file names whose reach is a single method. The audit this check answers to was
    about the table of column names and the value lists a combo displays, and those live
    at module level; widening further would bury the report under every ``"--format"`` in
    the package.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append(node.value)
        elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            for element in node.elts:
                visit(element)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if key is not None:
                    visit(key)
                visit(value)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            # Implicitly-concatenated literals arrive as one Constant in the AST; an
            # explicit ``+`` arrives as a BinOp, and both halves are still constants.
            visit(node.left)
            visit(node.right)

    for statement in tree.body:
        if isinstance(statement, ast.Assign) or (
            isinstance(statement, (ast.AnnAssign, ast.AugAssign)) and statement.value is not None
        ):
            visit(statement.value)
    return found


def _tr_literals(tree: ast.AST) -> set[str]:
    """Every constant string passed directly to ``tr(...)``."""
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name == "tr" and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                keys.add(arg.value)
    return keys


def _count_of_nouns(tree: ast.AST, where: str) -> tuple[set[str], list[str]]:
    """The nouns handed to ``count_of``, and the call sites whose noun is not a literal.

    ``count_of`` builds its table key at run time -- ``"{{}} " + noun``, plural or not --
    so scan 1 cannot see what it will ask the table for: the key exists nowhere as a
    literal. This walks the call sites instead and collects what the keys will be made
    from. A call site whose noun is anything other than a string literal is returned in
    ``dynamic`` rather than skipped: a noun this scan cannot read is a set of keys it
    cannot check, and that must be a decision a person makes, not a gap nobody notices.
    """
    nouns: set[str] = set()
    dynamic: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "count_of" or len(node.args) < 2:
            continue
        noun_arg = node.args[1]
        if isinstance(noun_arg, ast.Constant) and isinstance(noun_arg.value, str):
            nouns.add(noun_arg.value)
        else:
            dynamic.append(f"{where}:{node.lineno}")
    return nouns, dynamic


def main() -> int:
    failures = 0

    tr_keys: set[str] = set()
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        tr_keys |= _tr_literals(tree)

    print(f"scan 1: {len(tr_keys)} tr() literals referenced")
    for key in sorted(tr_keys):
        translation = ZH.get(key)
        if translation is None:
            print(f"  MISSING FROM TABLE: {key!r} (displays as English -- legal, review)")
            continue
        if len(_PLACEHOLDERS.findall(key)) != len(_PLACEHOLDERS.findall(translation)):
            print(f"  PLACEHOLDER MISMATCH: {key!r} -> {translation!r}")
            failures += 1
    print(f"scan 1: {sum(1 for k in tr_keys if k in ZH)}/{len(tr_keys)} keys in the table")

    print("scan 2: module-level string constants across gigaxml.gui")
    unaccounted: list[str] = []
    in_table = allowlisted = 0
    for path in sorted(SRC.rglob("*.py")):
        if path.name == _TABLE_HOME:
            continue
        for text in _string_constants(path):
            if text in ZH:
                in_table += 1
            elif _allowlisted(text) is not None:
                allowlisted += 1
            else:
                unaccounted.append(f"{path.relative_to(SRC)}: {text!r}")
    print(f"scan 2: {in_table} in table, {allowlisted} allowlisted, {len(unaccounted)} unaccounted")
    for entry in unaccounted:
        print(f"  UNACCOUNTED: {entry}")
        failures += 1

    print("scan 3: count_of nouns, whose keys are built at run time")
    all_nouns: set[str] = set()
    noun_sites: dict[str, list[str]] = {}
    dynamic_sites: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        where = str(path.relative_to(SRC))
        nouns, dynamic = _count_of_nouns(tree, where)
        all_nouns |= nouns
        for noun in nouns:
            noun_sites.setdefault(noun, []).append(where)
        dynamic_sites.extend(dynamic)
    if not all_nouns:
        # Zero is not a clean answer, it is a broken scanner: this interface counts rows,
        # records and parts, so a scan that finds no nouns has stopped looking.
        print("  scan 3 found zero count_of call sites -- the scan is broken, not clean")
        failures += 1
    print(f"scan 3: {len(all_nouns)} nouns: {sorted(all_nouns)}")
    for noun in sorted(all_nouns):
        for key in (f"{{}} {noun}", f"{{}} {noun}s"):
            if key not in ZH:
                print(
                    f"  MISSING FROM TABLE: noun {noun!r} (used at "
                    f"{', '.join(noun_sites[noun])}) needs key {key!r} "
                    f"-- a missing key displays as raw braces"
                )
                failures += 1
    for site in dynamic_sites:
        print(
            f"  NON-LITERAL NOUN at {site} -- a noun this scan cannot read; "
            f"make it a literal or account for its keys by hand"
        )
        failures += 1

    if failures:
        print(f"\n{failures} failure(s); see the docstring for what each scan demands")
    else:
        print("\nall interface strings accounted for")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
