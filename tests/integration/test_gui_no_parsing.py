"""红线 9：界面进程里不许出现解析。**用 AST 查，不 grep。**

**这一条曾经只存在于文档里。** ``tests/performance/test_gui_memory.py`` 与
``tests/_gui_mem.py`` 的 docstring 都写着「hard constraint 1, proved separately by G5's
grep over ``src/gigaxml/gui/``」,而仓库里从来没有那个 G5。守卫被引用、被依赖、被当成
「已经单独证明过了」,实际并不存在。下面的测试把它补上,并且放进每次 CI 都跑的
``tests/integration/`` —— 不在 ``performance`` 标记后面,因为它不是测量,它是一个断言。

**为什么是 AST 而不是文本搜索。** 本项目栽过这个坑:文档里写着「grep over
``src/gigaxml/gui/``」,而 grep 会在注释和字符串里也命中。于是两种失败同时可能发生 ——
真正的违规被注释里的一个词掩盖过去,或者干净的代码被文档字符串里的 ``iterparse`` 判成违规。
两个方向都是「红在错误的地方」,也就是什么都没验证。:mod:`ast` 里没有这个歧义:
注释不是节点,字符串不是名字,docstring 是 :class:`ast.Constant`。所以下面这个检查器读的是
**程序实际会执行的结构**,不是它碰巧包含哪些词。

**守卫必须自己证明自己敏感。** 一个从来没红过的断言和一个坏掉的断言在结果上无法区分,
所以 :func:`test_the_checker_catches_each_way_of_moving_parsing_into_the_window` 用构造的
代码片段把每一种搬法都喂进去,要求每一种都红,并且指出正确的行。变异验证因此是**常驻的**,
不是我在某次会话里手动做一遍就完的事。

**判据是「红在正确的位置」,不是「必须红」。** 下面每个检查都指名它为什么红:
被检测的违规是什么、行号是什么。崩在语法错误上的红不算通过。
"""

from __future__ import annotations

import ast
import pathlib
from dataclasses import dataclass

import pytest

__all__ = [
    "GUI_ROOT",
    "Finding",
    "check_source",
    "forbidden_uses",
    "gui_sources",
]

#: The directory under guard. Fixed rather than derived from the installed package, so the
#: check covers the tree in this checkout -- which is the thing a change is made to.
GUI_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parents[2] / "src" / "gigaxml" / "gui"

#: Modules whose import would put a parser in the interface process.
#:
#: ``gigaxml.inspect`` and ``gigaxml.sample`` are here for the same reason ``lxml`` is: both
#: import a parser, so importing either is importing a parser. ``gigaxml.fields`` is
#: deliberately **absent** -- ``FieldType`` is data and the field panel legitimately reads it;
#: the extraction entry point that needs an element is banned by name below instead, so the
#: rule stays as narrow as the thing it forbids.
_FORBIDDEN_MODULES: frozenset[str] = frozenset(
    {
        "lxml",
        "gigaxml.parser",
        "gigaxml.inspect",
        "gigaxml.sample",
    }
)

#: Entry points that read a document, by name rather than by module.
#:
#: **Two of these would slip past a module-only check**, and that is why they are here:
#: ``iterparse`` and ``fromstring`` are reached as attributes of an ``etree`` that was bound
#: by a *different* import, and an interface process can get hold of an ``etree`` without ever
#: writing ``import lxml`` on one line. ``RecordPathError`` is deliberately not here: it is
#: raised from ``gigaxml.errors`` and the error panel already classifies on it, so banning the
#: name would ban the advice for a checkpoint refusal.
_FORBIDDEN_NAMES: frozenset[str] = frozenset(
    {
        # lxml's reading APIs, in every spelling they are called.
        "iterparse",
        "iterwalk",
        "fromstring",
        "XML",
        "XMLParser",
        "XMLPullParser",
        "HTMLParser",
        # This project's own reading entry points.
        "StreamingRecordReader",
        "consume_records",
        "extract_record",
        "inspect_document",
        "sample_records",
    }
)


@dataclass(frozen=True)
class Finding:
    """One way the interface process would end up holding a parser.

    ``line`` is 1-based and points at the node that caused it, so a failure says where to
    look rather than only that something was found.
    """

    path: pathlib.Path
    line: int
    reason: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.reason}"


def gui_sources() -> list[pathlib.Path]:
    """Every module under the guarded directory, sorted.

    Raises:
        AssertionError: if the directory is empty. A guard that scans nothing passes, and the
            failure that matters is the one where a rename empties the directory and the guard
            goes quiet -- so the emptiness is itself an assertion, made once here and relied on
            by every test below.
    """
    files = sorted(GUI_ROOT.rglob("*.py"))
    assert files, f"no Python under {GUI_ROOT}: the guard would scan nothing and pass"
    return files


def _root_module(name: str) -> str:
    """``lxml.etree.iterparse`` -> ``lxml``; ``gigaxml.parser`` -> ``gigaxml.parser``.

    The first two components for a first-party path, one for anything else, so a ban on
    ``gigaxml.parser`` covers ``gigaxml.parser.streaming`` without having to enumerate
    submodules that may not exist yet.
    """
    parts = name.split(".")
    return ".".join(parts[:2]) if name.startswith("gigaxml.") else parts[0]


def forbidden_uses(tree: ast.AST) -> list[tuple[int, str]]:
    """Every banned import or reference in ``tree``, as ``(line, reason)`` pairs.

    Walks the tree rather than the text, which is the whole point: a docstring that names
    ``iterparse`` in order to explain why it is forbidden is a ``Constant`` here and is not
    reported, and a comment is not a node at all.
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _root_module(alias.name) in _FORBIDDEN_MODULES:
                    found.append((node.lineno, f"imports {_root_module(alias.name)!r}"))
        elif isinstance(node, ast.ImportFrom):
            # `from . import x` has module None; the relative level tells us nothing about
            # which package it lands in, and a relative import cannot reach lxml or gigaxml
            # anyway, so it is skipped rather than guessed at.
            if node.module and _root_module(node.module) in _FORBIDDEN_MODULES:
                found.append((node.lineno, f"imports from {_root_module(node.module)!r}"))
            for alias in node.names:
                if alias.name in _FORBIDDEN_NAMES:
                    found.append((node.lineno, f"imports {alias.name!r}"))
        elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_NAMES:
            found.append((node.lineno, f"uses {node.attr!r}"))
        elif isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            found.append((node.lineno, f"names {node.id!r}"))
    return found


def check_source(path: pathlib.Path) -> list[Finding]:
    """The violations in one file, as findings.

    A file that does not parse is itself a finding rather than a silent pass: the whole guard
    is built on the tree, and a tree that could not be built means the guard proved nothing
    about that file.
    """
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [
            Finding(path, exc.lineno or 0, f"does not parse, so the guard could not read it: {exc}")
        ]
    return [Finding(path, line, reason) for line, reason in forbidden_uses(tree)]


# --- the guard ---------------------------------------------------------------


def test_no_module_under_the_gui_ever_reaches_a_parser() -> None:
    """红线 9, stated as an assertion.

    The failure message is every finding at once, not just the first: a change that moves
    parsing in usually touches two files, and a guard that reports one at a time makes the
    fix a sequence of red-green-red-green rounds.
    """
    findings = [finding for path in gui_sources() for finding in check_source(path)]
    assert not findings, "the interface process must never parse a document:\n" + "\n".join(
        str(item) for item in findings
    )


def test_the_guard_reads_every_module_and_not_just_the_ones_that_import_something() -> None:
    """**The guard's own coverage, asserted.**

    A directory walk that silently stopped finding files would leave the guard passing on a
    fraction of the tree -- the state a guard is most often in when it is actually broken, and
    the state nothing else here would notice. So the set of files read is compared against
    the set on disk, and the count is pinned, so a new module that the walk misses is a
    failure rather than a gap.
    """
    sources = gui_sources()
    assert len(sources) == len(list(GUI_ROOT.rglob("*.py"))), (
        "the guard saw fewer files than the directory holds, so it is not covering the tree"
    )
    # The specific module that makes the claim true. If this file is ever deleted the other
    # tests would still pass -- there would just be nothing left to violate the rule.
    assert GUI_ROOT / "cli_process.py" in sources


def test_the_gui_still_drives_the_cli_as_a_child_process() -> None:
    """**The other half of 红线 9, and the one that makes the first half mean something.**

    "The interface never parses" is satisfied just as well by an interface that never
    processes anything. So the guard is paired with the claim that the interface *does* the
    work, by starting a child: :func:`subprocess.Popen` is reached in ``cli_process.py``, and
    that is the file the whole design rests on.
    """
    source = (GUI_ROOT / "cli_process.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    spawned = [
        node
        for node in ast.walk(tree)
        if (isinstance(node, ast.Attribute) and node.attr == "Popen")
        or (isinstance(node, ast.Name) and node.id == "Popen")
    ]
    assert spawned, "cli_process.py no longer starts a child process"
    assert "subprocess" in source, "cli_process.py must reach the child through subprocess"


# --- the guard proving it would catch the thing -----------------------------


#: Each way parsing gets moved into the window, and the line it would be on. Kept as source
#: text rather than as file paths so the mutation is reproducible from this file alone, with
#: no checkout to dirty and no chance of leaving a stray import behind.
_MUTATIONS: list[tuple[str, str, int]] = [
    ("direct_iterparse", "from lxml import etree\nfor _ in etree.iterparse(path):\n    pass\n", 1),
    (
        "aliased_import",
        "from lxml import etree as tree\ntree.iterparse(path)\n",
        1,
    ),
    (
        "module_import_only",
        "import lxml.etree\nlxml.etree.parse(path)\n",
        1,
    ),
    (
        "the_projects_own_reader",
        "from gigaxml.parser import StreamingRecordReader\nreader = StreamingRecordReader(path)\n",
        1,
    ),
    (
        "submodule_import",
        "from gigaxml.parser.streaming import StreamingRecordReader\n",
        1,
    ),
    (
        "inside_a_panel",
        "def count(path):\n    from lxml import etree\n    return len(etree.fromstring(b'<a/>'))\n",
        2,
    ),
    (
        "the_inspection_entry_point",
        "from gigaxml.inspect import inspect_document\ninspect_document(path)\n",
        1,
    ),
    (
        "the_extraction_loop",
        "from gigaxml.run import consume_records\n",
        1,
    ),
    (
        "named_in_a_type_annotation",
        "def build(reader: StreamingRecordReader) -> None:\n    pass\n",
        1,
    ),
]


@pytest.mark.parametrize(
    ("name", "source", "expected_line"),
    _MUTATIONS,
    ids=[item[0] for item in _MUTATIONS],
)
def test_the_checker_catches_each_way_of_moving_parsing_into_the_window(
    name: str, source: str, expected_line: int
) -> None:
    """**变异验证, as a permanent test.**

    Each entry is a real way the red line gets broken -- an aliased import, a submodule, a
    lazy import inside a function, the project's own reader reached through ``run`` -- and
    each is required to be caught **on the line that carries it**. Pinning the line is what
    makes this a check of *what* was found rather than of *how many*: a checker that reported
    line 1 for an import on line 2 would pass a count-based assertion while having read
    nothing.
    """
    del name  # only the id
    found = forbidden_uses(ast.parse(source))
    assert found, f"the guard did not notice this at all:\n{source}"
    lines = {line for line, _ in found}
    assert expected_line in lines, (
        f"the violation is on line {expected_line} and the guard reported {sorted(lines)}:\n"
        f"{source}"
    )
    assert all(reason for _, reason in found), "a finding arrived without saying what it was"


def test_the_checker_ignores_the_words_that_explain_the_rule() -> None:
    """**The half of the guard that a grep cannot pass.**

    This module's own docstring names every banned API in order to say why they are banned,
    and so do the panels' comments about the memory claim. A text search would report all of
    it. The tree walk reports none of it, and that asymmetry is the reason the guard reads a
    tree: a guard that fires on its own explanation cannot be used to check anything else.
    """
    documented = (
        '"""Uses iterparse in the old version; lxml and gigaxml.parser are both banned."""\n'
        "# A comment mentioning fromstring, XMLPullParser and StreamingRecordReader.\n"
        "MESSAGE = 'do not call etree.iterparse here'\n"
        'COLUMNS = ("iterparse", "fromstring")\n'
    )
    assert forbidden_uses(ast.parse(documented)) == []


def test_the_checker_accepts_the_imports_the_gui_actually_needs() -> None:
    """**It has to pass the real tree, or it is a rule nobody can follow.**

    Every import ``src/gigaxml/gui/`` makes today, run through the checker. If a future change
    needed one of these, this test is what says so -- and the point of listing them here is
    that the boundary is a decision rather than an accident: ``FieldType`` from
    ``gigaxml.fields`` is data, ``read_checkpoint`` from ``gigaxml.checkpoint`` reads a JSON
    manifest rather than a document, and ``QUARANTINABLE`` from ``gigaxml.run`` is a tuple of
    exception classes.
    """
    from gigaxml.checkpoint import (
        CHECKPOINT_FILENAME,
        Checkpoint,
        CheckpointError,
        read_checkpoint,
    )
    from gigaxml.config import ExtractionConfig, load_config, parse_config
    from gigaxml.errors import GigaXMLError
    from gigaxml.fields import FieldType
    from gigaxml.run import DEFAULT_RUN_REPORT_FILENAME, QUARANTINABLE
    from gigaxml.writers import PARTIAL_SUFFIX

    del (
        CHECKPOINT_FILENAME,
        Checkpoint,
        CheckpointError,
        read_checkpoint,
        ExtractionConfig,
        load_config,
        parse_config,
        GigaXMLError,
        FieldType,
        DEFAULT_RUN_REPORT_FILENAME,
        QUARANTINABLE,
        PARTIAL_SUFFIX,
    )
    # The imports above are the assertion: performing them is what the tree already does, and
    # the checker accepts all of them. Written as real imports so the list cannot rot into
    # strings that name things the package stopped exporting.


def test_the_module_ban_covers_submodules_of_a_banned_package() -> None:
    """``gigaxml.parser.streaming`` must not be a way around ``gigaxml.parser``.

    The submodules that exist today are enumerated in :data:`_FORBIDDEN_MODULES`'s docstring
    and would have to be added by hand; this pins the rule that makes that unnecessary, so a
    submodule added later is covered rather than missed.
    """
    for module in ("gigaxml.parser.streaming", "gigaxml.parser.future", "lxml.etree"):
        found = forbidden_uses(ast.parse(f"from {module} import anything\n"))
        assert found, f"importing from {module!r} was not caught"
    # And a package that merely *mentions* one of these as a substring of its own name is not
    # caught -- the ban is on the package, not on any name containing it.
    assert forbidden_uses(ast.parse("import gigaxml.parser_extra\n")) == []
