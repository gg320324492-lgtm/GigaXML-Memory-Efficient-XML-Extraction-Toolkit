"""Integration tests: what "atomically" is allowed to mean.

Three layers, and the milestone exists because the word sat in four places with no
definition anywhere. Layer 1 and layer 2 are promised; layer 3 is not. These tests pin
all three, so that "M11 is done" stops being a claim and becomes a fact that can go red.

★ **The mechanism tests are what make the document tests worth having.** A test that
checks the prose says "never a mixture" is worthless if the prose has drifted away from
the code. So the mechanism is run: one test spies on every rename the package performs
and checks where the two paths sit, and one races a reader against a live publish, because
that is the only way to *observe* layer 2 rather than assert it.
"""

from __future__ import annotations

import ast
import contextlib
import pathlib
import re
import subprocess
import threading
import time
from typing import NamedTuple

import pytest

from gigaxml import ExtractionConfig, StreamingRecordReader, create_writer, load_config
from gigaxml.checkpoint import Checkpoint, write_checkpoint
from gigaxml.errors import WriterError

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCUMENT = ROOT / "OUTPUT-DURABILITY.md"

#: The words that promise more than this project promises.
#:
#: ``fsync`` is in the list deliberately: naming the syscall is how a reader is most
#: likely to be told a guarantee exists, whether or not one was made.
_DURABILITY_WORD = re.compile(
    r"\b(durab\w*|surviv\w*|crash[- ]proof|power[- ]loss|fsync\w*)\b", re.I
)


# --- the mechanism, so the document is a claim about code ---------------------------


def _config(tmp_path: pathlib.Path) -> ExtractionConfig:
    (tmp_path / "config.yaml").write_text(
        "record: /catalog/products/product\n"
        "fields:\n"
        "  product_id:\n    path: '@id'\n"
        "  name:\n    path: name\n",
        encoding="utf-8",
    )
    return load_config(tmp_path / "config.yaml")


def _row(index: int) -> dict[str, object]:
    return {"product_id": str(index), "name": f"row-{index}-with-enough-text-to-be-distinct"}


@pytest.fixture
def renames(monkeypatch: pytest.MonkeyPatch) -> list[Rename]:
    """Every ``Path.replace`` the package performs, with the device read *before* it happens.

    ★ One spy rather than five site-specific tests, because the claim is the same at every
    site and a fifth copy of it would be a fifth thing to keep true. It also cannot be
    fooled by a site that starts publishing through some other route: the spy sees the
    rename, not the code path taken to reach it.

    ★ The devices are captured here rather than in the assertion because by the time the
    test looks, the source is gone -- ``os.replace`` moved it -- and ``os.stat`` on a file
    that a rename has already consumed fails with ``FileNotFoundError``, which is a
    confusing way to learn that the publish worked.
    """
    seen: list[Rename] = []
    real = pathlib.Path.replace

    def spy(self: pathlib.Path, target: object) -> pathlib.Path:
        destination = pathlib.Path(target)
        seen.append(
            Rename(
                source=self,
                target=destination,
                source_device=self.stat().st_dev,
                target_device=destination.parent.stat().st_dev,
            )
        )
        return real(self, target)

    monkeypatch.setattr(pathlib.Path, "replace", spy)
    return seen


class Rename(NamedTuple):
    """One publish: where the bytes came from, where they went, and which devices."""

    source: pathlib.Path
    target: pathlib.Path
    source_device: int
    target_device: int

    @property
    def beside(self) -> bool:
        return self.source.parent == self.target.parent


def _assert_published_beside(seen: list[Rename], label: str) -> None:
    assert seen, f"{label} published nothing at all, so there is no rename to check"
    rename = seen[-1]
    assert rename.beside, (
        f"{label} renamed from {rename.source} onto {rename.target}: the partial file must "
        "be written beside the target, because os.replace across devices is a copy plus a "
        "delete, which is not atomic"
    )
    # "Same directory" is a weaker claim than "same device", and it is the weaker one that
    # breaks silently: a mount point or a bind mount inside the directory satisfies the
    # first and still lands the rename on two devices.
    assert rename.source_device == rename.target_device, (
        f"{label}: same directory is not the same claim as same device, and layer 2 needs "
        f"the device ({rename.source} vs {rename.target})"
    )


def test_the_writer_publishes_a_sibling_onto_the_target(
    tmp_path: pathlib.Path, renames: list[Rename]
) -> None:
    """Criterion D for the extracted output -- the site that matters most."""
    config = _config(tmp_path)
    target = tmp_path / "out.csv"
    writer = create_writer(target, config.fields)
    writer.write(_row(1))

    assert writer.partial_path.parent == target.parent
    assert not renames, "a writer that has not been closed must not have published anything"

    writer.close()

    _assert_published_beside(renames, "the writer")
    assert target.read_text(encoding="utf-8").splitlines() == [
        "product_id,name",
        "1,row-1-with-enough-text-to-be-distinct",
    ]
    assert not writer.partial_path.exists(), "a published run leaves no partial behind"


def test_the_checkpoint_manifest_publishes_a_sibling_onto_the_target(
    tmp_path: pathlib.Path, renames: list[Rename]
) -> None:
    """★ A half-written manifest is worse than none: the next ``--resume`` would trust it."""
    source = tmp_path / "doc.xml"
    source.write_text("<catalog/>", encoding="utf-8")
    target = tmp_path / "run.ckpt.json"
    checkpoint = Checkpoint(
        source={"path": str(source), "size": 11, "sha256": "0" * 64},
        config="a-config-hash",
        records_consumed=0,
        rejected=0,
        parts=(),
        complete=False,
        version=1,
    )
    write_checkpoint(target, checkpoint)

    _assert_published_beside(renames, "the checkpoint manifest")
    assert target.exists()
    leftovers = sorted(p.name for p in tmp_path.iterdir())
    assert leftovers == ["doc.xml", "run.ckpt.json"], (
        f"a published manifest leaves only itself and its inputs behind: {leftovers}"
    )


def test_every_gui_store_publishes_a_sibling_onto_its_target(
    tmp_path: pathlib.Path, renames: list[Rename]
) -> None:
    """The three stores the window writes use the same mechanism, so they get the same test.

    Skipped without PySide6, and the skip is honest rather than convenient: the static
    guard below still covers these files on a machine with no Qt at all.
    """
    pytest.importorskip("PySide6", reason="the GUI stores live in the optional desktop extra")
    from gigaxml.gui.job_history import JobHistory
    from gigaxml.gui.recent_files import RecentFiles
    from gigaxml.gui.settings import Settings, SettingsStore

    renames.clear()
    SettingsStore(tmp_path / "settings.json").write(Settings())
    _assert_published_beside(renames, "SettingsStore")

    recent_entry = tmp_path / "a-file.xml"
    recent_entry.write_text(
        "<catalog/>", encoding="utf-8"
    )  # a remembered path points at a real file
    renames.clear()
    RecentFiles(tmp_path / "recent.json").add(recent_entry)
    _assert_published_beside(renames, "RecentFiles")

    renames.clear()
    JobHistory(tmp_path / "history.json").note(tmp_path / "a-file.xml", checkpointing=False)
    _assert_published_beside(renames, "JobHistory")

    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "a-file.xml",
        "history.json",
        "recent.json",
        "settings.json",
    ], "every store published and cleaned up its own partial file"


def test_every_publish_site_derives_its_partial_path_with_with_name() -> None:
    """★ Criterion D as a structural rule, and the guard that works without Qt installed.

    The rule this asserts is the one that actually makes layer 2 unconditional:
    **the partial path is the target's own path with a suffix added to its name.**
    ``Path.with_name`` puts the result in the same directory by construction, so there is
    no branch where the two could end up on different volumes.

    ★ **Why the rule is about ``with_name`` and not merely about ``tempfile``.** A first
    draft of this test flagged any module that both renamed and mentioned ``tempfile``,
    which would have fired on ``tempfile.mkstemp(dir=target.parent)`` -- a *correct*
    implementation. A guard that fails on correct code trains people to route around it,
    which is the M9 lesson about bare-name matching all over again. This version states the
    property instead of the taboo, so an unusual-but-correct implementation fails with a
    message that says what to do about it.

    The companion runtime check is the ``st_dev`` assertion in
    :func:`_assert_published_beside`. Neither alone is enough, and the mutation runs in
    ``docs/STAGE3-M11-REPORT.md`` show why: on this machine the temporary directory and the
    test directory share a device, so the behavioural check alone would have stayed green
    against a writer that published from somewhere else entirely.
    """
    offenders: list[str] = []
    for path in sorted((ROOT / "src" / "gigaxml").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        publishes = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "replace"
            # One positional argument is ``Path.replace(target)``, which is the publish.
            # Two is ``str.replace(old, new)``, which is text munging and not a publish --
            # the discriminator matters, because a text munger in the same module would
            # otherwise make every module look like a publish site.
            and len(node.args) == 1
            and not node.keywords
            for node in ast.walk(tree)
        )
        if not publishes:
            continue
        derives = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "with_name"
            for node in ast.walk(tree)
        )
        uses_temp = any(
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "tempfile"
            for node in ast.walk(tree)
        )
        if not derives or uses_temp:
            offenders.append(relative)
    assert offenders == [], (
        "every module that publishes by renaming a partial file must build that file with "
        "Path.with_name(target.name + suffix) and must not reach for tempfile: a temporary "
        "path is how a publish silently moves off the target's device. Offending modules: "
        f"{offenders}"
    )


def test_a_reader_never_observes_a_mixed_target(tmp_path: pathlib.Path) -> None:
    """★ Layer 2 observed rather than asserted, by racing a reader against the publish.

    Every other mechanism test checks that the code *says* the target is never a mixture.
    This one watches: a thread reads the target in a tight loop while the writer republishes
    it with clearly distinguishable content, and every byte string the reader ever saw must
    be one whole version.

    Windows refuses ``os.replace`` onto a file another handle has open (``WinError 5``) --
    the behaviour ``writers.py`` already reports to users -- so a publish can lose that
    race. That is not a flake to be retried away: it is the guarantee showing its other
    side, because the target is then left holding the *previous* complete version, which
    this test counts as an observation rather than an error.
    """
    config = _config(tmp_path)
    target = tmp_path / "out.csv"
    header = "product_id,name\n"
    published: set[str] = set()
    observed: list[str] = []
    stop = threading.Event()

    def publish(content: str) -> bool:
        writer = create_writer(target, config.fields)
        writer.write({"product_id": content, "name": content})
        try:
            writer.close()
        except WriterError:
            return False  # the reader held it open; the previous version stands
        published.add(content)
        return True

    # The first version stays in `published`: the watcher is racing publishes two through
    # fifteen, and a reader that catches version-000 has observed a whole file like any
    # other. Clearing it here would count a correct observation as a mixture.
    assert publish("version-000"), "the first publish has no reader to race"

    def watch() -> None:
        while not stop.is_set():
            # A Windows sharing violation raises OSError: that attempt observed nothing,
            # which is not a failure -- it is the reader losing a race it was not betting on.
            with contextlib.suppress(OSError):
                observed.append(target.read_text(encoding="utf-8"))
            time.sleep(0.001)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        for index in range(1, 16):
            content = f"version-{index:03d}-{'x' * (index * 7)}"
            for _ in range(3):  # a publish may lose the race; try again rather than give up
                if publish(content):
                    break
            time.sleep(0.01)
    finally:
        stop.set()
        watcher.join(timeout=5)

    assert len(published) >= 2, (
        f"the race produced too few publishes to conclude anything: {published}"
    )
    assert observed, "the watcher never managed to read the target"
    assert all(observed), (
        "the target was observed empty at some point, which is layer 2 failing too"
    )

    whole = {header + f"{version},{version}\n" for version in published}
    mixtures = sorted({text for text in observed if text not in whole})
    assert not mixtures, (
        f"layer 2 says a reader never sees a mixture; it saw {len(mixtures)}, first was "
        f"{mixtures[0][:120]!r}"
    )


def test_a_first_run_that_fails_leaves_no_target_at_all(tmp_path: pathlib.Path) -> None:
    """★ Layer 1's edge, and the one its wording is easiest to get wrong.

    "The target keeps the previous file" is true and vacuous when there *was* no previous
    file. What a reader must not infer is that the target always exists afterwards.
    """
    config = _config(tmp_path)
    target = tmp_path / "out.csv"
    writer = create_writer(target, config.fields)
    writer.write(_row(1))
    writer.abandon()  # what __exit__ does when the block raised

    assert not target.exists(), "a first run that failed published something"
    assert writer.partial_path.exists(), "the partial output is kept, where the report names it"
    assert not writer.published


def test_a_failed_rerun_leaves_the_previous_output_byte_for_byte(tmp_path: pathlib.Path) -> None:
    """Layer 1 when there *is* a previous version, asserted on bytes rather than on size."""
    config = _config(tmp_path)
    target = tmp_path / "out.csv"
    first = create_writer(target, config.fields)
    first.write(_row(1))
    first.close()
    before = target.read_bytes()

    second = create_writer(target, config.fields)
    second.write(_row(2))
    second.abandon()

    assert target.read_bytes() == before, "a failed re-run changed the previous output"


def test_the_publish_replaces_the_file_rather_than_extending_it(tmp_path: pathlib.Path) -> None:
    """Layer 2's mechanism on a real extraction: the inode changes, so it is a rename.

    Rewriting the target in place would pass every other test in this file -- the content
    would be right either way -- and would still be the mechanism layer 2 does not promise.
    """
    config = _config(tmp_path)
    (tmp_path / "doc.xml").write_text(
        "<catalog><products>"
        '<product id="1"><name>Alpha</name></product>'
        '<product id="2"><name>Beta</name></product>'
        "</products></catalog>",
        encoding="utf-8",
    )
    target = tmp_path / "out.csv"
    reader = StreamingRecordReader(tmp_path / "doc.xml", config.record_path, config.namespaces)
    writer = create_writer(target, config.fields)
    for record in reader:
        writer.write({"product_id": record.get("id"), "name": record.findtext("name")})
    before_publish = writer.partial_path.stat().st_ino

    writer.close()

    assert target.read_text(encoding="utf-8").splitlines() == [
        "product_id,name",
        "1,Alpha",
        "2,Beta",
    ]
    assert writer.published
    assert not writer.partial_path.exists()
    assert target.stat().st_ino == before_publish, (
        "the target is the renamed partial file, not a rewritten copy of it"
    )


def test_the_run_report_is_written_in_place_and_the_document_says_so() -> None:
    """★ The scope boundary, pinned in both directions.

    The run report is written straight onto its path with no partial file, so it gets none
    of layers 1 and 2. A reader who took "every output lands atomically" to cover every
    artefact this tool writes would be wrong about exactly this one -- which is why the
    document has a section saying so, and why this test fails if the code or the document
    changes without the other following.
    """
    import inspect

    from gigaxml.cli_pkg import common

    source = inspect.getsource(common.write_run_report)
    assert ".tmp" not in source and "replace" not in source, (
        "the run report is now published atomically. That is good news, and it means "
        "OUTPUT-DURABILITY.md is out of date: update its scope table rather than leaving "
        "it to contradict the code."
    )
    assert "written in place" in _section(DOCUMENT, "Where this document does not apply").lower()


# --- the document, which is the milestone's actual deliverable ---------------------


def _normalised(text: str) -> str:
    """Collapse whitespace, so a line break inside a sentence does not hide it.

    ★ Learned the hard way in M10: a licence sentence wrapping as ``is not part of the
    <newline> contract`` was invisible to a literal search, so the test meant to police it
    silently passed a document that did not have it. A reader does not see a line break in
    the middle of a sentence, so neither does this.
    """
    return re.sub(r"\s+", " ", text)


def _sections(document: pathlib.Path) -> dict[str, str]:
    """Every ``##``-level section, keyed by heading text.

    Split on the heading rather than searching for phrases, so that **deleting a section
    removes the key** instead of leaving its phrases behind somewhere else in the file.
    That is the difference between a guard that notices and one that cannot.
    """
    text = document.read_text(encoding="utf-8")
    parts = re.split(r"^## ", text, flags=re.MULTILINE)[1:]
    return {_normalised(part.splitlines()[0]): _normalised(part) for part in parts}


def _section(document: pathlib.Path, starts_with: str) -> str:
    for heading, body in _sections(document).items():
        if heading.startswith(starts_with):
            return body
    raise AssertionError(f"OUTPUT-DURABILITY.md has no section starting {starts_with!r}")


def _lead(document: pathlib.Path, starts_with: str) -> str:
    """The first paragraph of a section -- where a reader's eye stops.

    ★ This exists because a first version of the layer-3 guard searched the whole section
    for the phrase "not promised" and stayed green when the section's opening verdict was
    deleted, because three *later* sentences used the same words. The mutation runs in
    ``docs/STAGE3-M11-REPORT.md``: F3a. A verdict belongs at the top of the section that
    carries it, and the guard now insists on that position rather than on the vocabulary.
    """
    raw = next(
        part
        for part in re.split(r"^## ", document.read_text(encoding="utf-8"), flags=re.MULTILINE)[1:]
        if _normalised(part.splitlines()[0]).startswith(starts_with)
    )
    body = raw.split("\n", 1)[1] if "\n" in raw else ""
    return _normalised(body.split("\n\n")[0])


def test_the_document_is_at_the_repository_root_and_names_all_three_layers() -> None:
    """Criterion A: the definition exists, is where a reader will look, and names all three."""
    assert DOCUMENT.exists(), "OUTPUT-DURABILITY.md is missing from the repository root"
    assert DOCUMENT.parent == ROOT, "it must sit beside CONFIG-FORMAT.md and python-api.md"
    headings = list(_sections(DOCUMENT))
    assert any(h.startswith("Layer 1") for h in headings), headings
    assert any(h.startswith("Layer 2") for h in headings), headings
    assert any(h.startswith("Layer 3") for h in headings), headings


def test_layer_one_says_a_failed_run_publishes_nothing() -> None:
    """Criterion F, first half: this wording has to be load-bearing, not decorative."""
    body = _normalised(_section(DOCUMENT, "Layer 1"))
    assert "it does not publish a partial result" in body.lower()
    assert "no file at all" in body.lower(), (
        "layer 1 must say what happens on a FIRST run. 'Keeps the previous file' is true "
        "and vacuous when there was none, and a reader must not infer the target exists."
    )


def test_layer_two_says_the_target_is_never_a_mixture() -> None:
    """Criterion F, second half."""
    body = _normalised(_section(DOCUMENT, "Layer 2"))
    assert "never a mixture" in body.lower()
    assert "os.replace" in body.lower()
    assert "same filesystem is a precondition" in body.lower(), (
        "layer 2's precondition belongs in the section that promises layer 2, not only in "
        "the scope table at the end of the document"
    )


def test_layer_three_says_power_loss_is_not_promised() -> None:
    """★ Layer 3 is the one that has to be explicit, because it is the one that is inferred.

    The verdict is checked in the section's **opening paragraph**, not anywhere in it: a
    reader who wants to know whether gigaxml survives a power cut reads the first two lines
    and stops, so a refusal buried in paragraph five has not been given to anybody.
    """
    lead = _lead(DOCUMENT, "Layer 3")
    assert "not promised" in lead.lower(), (
        f"layer 3 must open by refusing the promise, not by explaining why later: {lead[:120]!r}"
    )
    assert "fsync" in lead.lower(), "and it must say so in the same breath"

    body = _normalised(_section(DOCUMENT, "Layer 3"))
    assert "you will not find a half-written file" in body.lower(), (
        "layer 3 must distinguish the two failure shapes: losing the whole file and "
        "finding a half-written one are different events, and only one of them is a lie."
    )
    assert "unverified" in body.lower(), (
        "the cross-platform part of layer 3 is unmeasured on this machine, and saying so in "
        "the section is the only thing keeping the rest of it honest."
    )


def test_the_document_separates_completeness_from_correctness() -> None:
    """★ The distinction a reader is most likely to miss, pinned where it is stated."""
    body = _normalised(_section(DOCUMENT, "Layer 1"))
    assert "does not say the file is *right*" in body.lower()
    assert "output_complete" in body.lower()


def test_the_document_states_the_measured_cost_so_nobody_re_guesses_it() -> None:
    """Criterion C's report content, kept as a fact rather than as a paragraph of prose.

    The cost of fsync turned out to be small, so the reason for not doing it is *not*
    performance. Left only in a report, the next person to raise it would reach for the
    estimate, find it plausible, and skip the measurement.
    """
    body = _normalised(_section(DOCUMENT, "If fsync is ever added"))
    assert "the cost is not the reason" in body.lower()
    assert "not a to-do list" in body.lower()


# --- criterion B: the wording in the rest of the package agrees --------------------


@pytest.mark.parametrize("relative", ["README.md", "src/gigaxml/writers.py"])
def test_every_atomic_claim_points_at_the_definition(relative: str) -> None:
    """Criterion B: saying "atomically" is allowed; leaving the reader to guess is not."""
    text = (ROOT / relative).read_text(encoding="utf-8")
    assert re.search(r"atomic", text, re.I), f"{relative} no longer claims atomicity at all"
    assert "OUTPUT-DURABILITY.md" in text, (
        f"{relative} says 'atomic' without pointing at the document that defines it, which "
        "is the exact failure M11 was raised to fix"
    )


#: Every durability word left in a shipped file, and why it is not a promise there.
#:
#: ★ A pin, not a silence. A file that points at OUTPUT-DURABILITY.md carries its own
#: judgement and is not listed; everything else needs a reason written next to it. Leaving
#: these unexamined is how a sixth one appears without anybody deciding to.
_PINNED_ELSEWHERE = {
    "src/gigaxml/cli_pkg/common.py": (
        "which of two colliding writes survives -- a race between two writes in one run, "
        "not survival across a power cut. cli_pkg is frozen by M11 section 3."
    ),
    "src/gigaxml/cli_pkg/sample_cmd.py": (
        "points at --checkpoint/--resume for an interrupted run, which the project does "
        "promise, and whose layer-3 boundary OUTPUT-DURABILITY.md states. Frozen by M11."
    ),
    "src/gigaxml/config.py": "a byte-order mark surviving as a character, in memory.",
    "src/gigaxml/fields.py": "a DOM element reference surviving, in memory.",
    "src/gigaxml/parser/streaming.py": "a parsed tree staying well-formed; frozen by M11.",
    "src/gigaxml/inspection/scanner.py": "as above; frozen by M11.",
    "src/gigaxml/gui/panels/fields.py": "a UI choice surviving a rebuild; frozen by M11.",
    "src/gigaxml/gui/panels/settings.py": (
        "a preference surviving a restart, not a power cut; frozen by M11."
    ),
    "packaging/RELEASE_NOTES.md": "settings surviving an upgrade, which is a support policy.",
}


def _shipped_files() -> list[str]:
    """What a reader can actually be sent to: the package, and the documents beside it."""
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    return [
        path
        for path in tracked
        if (path.startswith("src/") and path.endswith(".py"))
        or (
            path.endswith(".md")
            and not path.startswith(("docs/", ".workbuddy"))
            and path != "OUTPUT-DURABILITY.md"  # the definition itself is not a claimant
        )
    ]


#: What counts as bounding a durability claim, checked *inside the paragraph that makes it*.
#:
#: ★ Per paragraph, not per file. A first version of this guard skipped any file that
#: mentioned OUTPUT-DURABILITY.md anywhere, so restoring "durable" to one docstring in
#: ``run.py`` stayed green because four other docstrings in the same file still linked the
#: boundary -- mutation F5 in ``docs/STAGE3-M11-REPORT.md``. A reader of that docstring sees
#: its paragraph and nothing else, so the paragraph is the unit that has to hold up.
_BOUNDING_PHRASES = (
    "output-durability.md",
    "not promised",
    "not durability",
    "not durable",
    "no fsync",
    "layer 3",
)


def _blocks(text: str) -> list[str]:
    """Blank-line-delimited paragraphs: the unit a reader actually reads."""
    return re.split(r"\n[ \t]*\n", text)


def _unbounded_files() -> dict[str, int]:
    """Shipped files with at least one durability claim that nothing in its own paragraph bounds."""
    counts: dict[str, int] = {}
    for relative in _shipped_files():
        text = (ROOT / relative).read_text(encoding="utf-8")
        paragraphs = [
            block
            for block in _blocks(text)
            if _DURABILITY_WORD.search(block)
            and not any(phrase in block.lower() for phrase in _BOUNDING_PHRASES)
        ]
        if paragraphs:
            counts[relative] = len(paragraphs)
    return counts


def test_every_shipped_durability_claim_is_bounded_where_it_is_made() -> None:
    """★ Criterion B's banned words, as an inventory rather than a ban.

    The rule is deliberately *not* "these words may not appear". ``survive`` is correct
    English for a DOM element, a GUI preference and two colliding writes, and banning it
    wholesale would be a rule that fires on correct code. What is required instead is that
    every paragraph promising durability either points at the layer that refuses it or
    refuses it in that same paragraph.
    """
    unaccounted = {
        relative: f"★ UNEXAMINED -- {count} unbounded paragraph(s)"
        for relative, count in _unbounded_files().items()
        if relative not in _PINNED_ELSEWHERE
    }
    assert not unaccounted, (
        "a durability claim with nothing bounding it in the same paragraph, and no recorded "
        f"judgement: {unaccounted}"
    )


def test_the_pinned_list_is_still_exactly_the_files_it_was() -> None:
    """★ A pin, not a silence: everything left is accounted for, and nothing new slips in.

    This one also fails when a pinned file stops needing a judgement, so the list has to be
    retired deliberately rather than going stale because the wording moved underneath it.
    """
    still_needed = _unbounded_files()
    unexplained = sorted(rel for rel in still_needed if rel not in _PINNED_ELSEWHERE)
    assert not unexplained, f"a durability word appeared with no recorded judgement: {unexplained}"

    vanished = sorted(set(_PINNED_ELSEWHERE) - set(still_needed))
    assert not vanished, (
        "these files no longer need a recorded judgement, so the pin is stale and something "
        f"changed without anyone deciding to -- check each and drop it: {vanished}"
    )
