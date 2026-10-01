"""Property tests: a checkpoint manifest is accepted or refused, never anything else.

**The invariant.** For any JSON value at all, ``read_checkpoint`` returns a
:class:`Checkpoint` or raises :class:`CheckpointError`.

★ **This is the file the milestone is really about, and here is why.** A manifest is
hostile input by construction: it lives on disk, it was not written by this process, and
``--resume`` will trust it to decide how many records to skip and which part files to
open. M2 replaced lenient coercion here with strict checking, and the tests written with
it cover the shapes its author could think of -- ``records_consumed: true``,
``version: 1.0``, a truncated sha256. The properties below ask the machine for the rest,
and the second half of the file re-states the *specific* rules by construction, so they
hold for every value of a shape rather than the two that were written down.

★ **The property worth most here is the second one.** ``read_checkpoint``'s docstring
claims "a stricter reader has to accept everything the existing writer produced". That is
a claim about two functions in the same module, and if it were false a user who
checkpointed a run and resumed it in the same version would be broken. It is a
round-trip property, so nothing short of running it will do.
"""

from __future__ import annotations

import json
import pathlib
import tempfile

from hypothesis import given
from hypothesis import strategies as st

from gigaxml.checkpoint import (
    CHECKPOINT_VERSION,
    Checkpoint,
    PartRecord,
    read_checkpoint,
    write_checkpoint,
)
from gigaxml.errors import CheckpointError

from .conftest import FILE_PROPERTY_SETTINGS, PROPERTY_SETTINGS, accepts_or_refuses

CHECKPOINT_ERRORS: tuple[type[BaseException], ...] = (CheckpointError,)

SHA256 = "0" * 64
SOURCE = {"path": "C:/data/catalog.xml", "size": 1024, "sha256": SHA256}


def manifest(**overrides: object) -> dict[str, object]:
    """A manifest this build would write, with named fields replaced."""
    base: dict[str, object] = {
        "version": CHECKPOINT_VERSION,
        "source": dict(SOURCE),
        "config": SHA256,
        "records_consumed": 10,
        "rejected": 2,
        "parts": [],
        "complete": False,
    }
    base.update(overrides)
    return base


def _write(tmp_path: pathlib.Path, payload: object) -> pathlib.Path:
    """Put one JSON value where ``read_checkpoint`` will find it. Never into the repo."""
    target = tmp_path / "run.ckpt.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


# --- any JSON at all ----------------------------------------------------------------

#: Every JSON value a manifest could carry. Arbitrary keys, arbitrary types, arbitrary
#: nesting -- including the values a YAML-shaped file could never produce but a hand-edited
#: or hostile one absolutely could.
JSON_VALUE = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**31), max_value=2**31)
    | st.text(max_size=10),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=5)
    ),
    max_leaves=10,
)


@FILE_PROPERTY_SETTINGS
@given(JSON_VALUE)
def test_any_json_is_accepted_or_refused_by_name(tmp_path: pathlib.Path, payload: object) -> None:
    """Criterion B and C over the whole space. Nothing else is an acceptable outcome."""
    target = _write(tmp_path, payload)
    accepts_or_refuses(
        lambda: read_checkpoint(target),
        expected=CHECKPOINT_ERRORS,
        label=f"read_checkpoint(<{type(payload).__name__}>)",
    )


@FILE_PROPERTY_SETTINGS
@given(st.dictionaries(st.text(max_size=8), JSON_VALUE, max_size=8))
def test_an_arbitrary_object_is_accepted_or_refused_by_name(
    tmp_path: pathlib.Path, payload: dict[str, object]
) -> None:
    """The space the hand-written tests do not reach: an object whose keys are arbitrary.

    A manifest with the right shape and one wrong key must be refused; one missing a key
    must be refused. Both are the same property seen from two directions.
    """
    target = _write(tmp_path, payload)
    checkpoint = accepts_or_refuses(
        lambda: read_checkpoint(target),
        expected=CHECKPOINT_ERRORS,
        label=f"read_checkpoint(<object with {sorted(payload)!r}>)",
    )
    if checkpoint is None:
        return
    assert set(checkpoint.to_dict()) == {
        "version",
        "source",
        "config",
        "records_consumed",
        "rejected",
        "parts",
        "complete",
    }, f"an accepted manifest is missing a promised key: {sorted(checkpoint.to_dict())!r}"


# --- the round trip the module claims ------------------------------------------------


#: ``(records_consumed, rejected)`` pairs a real run can produce. The bound is not a
#: convenience: ``read_checkpoint`` refuses a manifest claiming more rejections than
#: records read, so an unconstrained pair would be asking the round trip to succeed on
#: states no run can reach. The asymmetry that produces is pinned separately below.
COUNTS = st.tuples(
    st.integers(min_value=0, max_value=2**31), st.integers(min_value=0, max_value=2**31)
).filter(lambda pair: pair[1] <= pair[0])


@FILE_PROPERTY_SETTINGS
@given(
    COUNTS,
    st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=99999),
            st.integers(min_value=0, max_value=2**31),
        ),
        max_size=4,
    ),
    st.booleans(),
)
def test_everything_the_writer_emits_the_reader_accepts(
    tmp_path: pathlib.Path,
    counts: tuple[int, int],
    parts: list[tuple[int, int]],
    complete: bool,
) -> None:
    """★ ``read_checkpoint``'s docstring, as a property rather than as a claim.

    "A stricter reader has to accept everything the existing writer produced." If that were
    false, checkpointing and resuming inside one version would be broken -- and no
    hand-written test covers it, because each of those tests either writes a manifest by
    hand or reads one that was written by hand.
    """
    records_consumed, rejected = counts
    records = [PartRecord(name=f"part-{index:05d}.parquet", rows=rows) for index, rows in parts]
    checkpoint = Checkpoint(
        source=dict(SOURCE),
        config=SHA256,
        records_consumed=records_consumed,
        rejected=rejected,
        parts=tuple(records),
        complete=complete,
        version=CHECKPOINT_VERSION,
    )
    target = tmp_path / "written.ckpt.json"
    write_checkpoint(target, checkpoint)

    read_back = read_checkpoint(target)

    assert read_back.to_dict() == checkpoint.to_dict(), (
        f"the writer and the reader disagree about their own format:\n"
        f"  written {checkpoint.to_dict()!r}\n  read    {read_back.to_dict()!r}"
    )


@FILE_PROPERTY_SETTINGS
@given(st.lists(st.integers(min_value=0, max_value=99999), min_size=1, max_size=5))
def test_part_rows_are_read_back_exactly(tmp_path: pathlib.Path, rows: list[int]) -> None:
    """Part row counts decide where a resumed run starts, so an off-by-one is a data bug."""
    checkpoint = Checkpoint(
        source=dict(SOURCE),
        config=SHA256,
        records_consumed=sum(rows),
        rejected=0,
        parts=tuple(
            PartRecord(name=f"part-{index:05d}.csv", rows=count) for index, count in enumerate(rows)
        ),
        complete=False,
        version=CHECKPOINT_VERSION,
    )
    target = tmp_path / "parts.ckpt.json"
    write_checkpoint(target, checkpoint)

    assert [part.rows for part in read_checkpoint(target).parts] == rows


# --- the specific refusals M2 introduced, by construction ---------------------------


@PROPERTY_SETTINGS
@given(
    st.sampled_from(
        [
            ("records_consumed", True),
            ("records_consumed", 1.0),
            ("records_consumed", "10"),
            ("rejected", True),
            ("rejected", "0"),
            ("records_consumed", -1),
            ("rejected", -1),
            ("version", True),
            ("version", 1.0),
            ("version", "1"),
            ("version", CHECKPOINT_VERSION + 1),
            ("config", "not-a-digest"),
            ("config", "A" * 64),  # uppercase hex
            ("config", SHA256[:-1]),  # one character short
            ("config", None),
            ("complete", "false"),
            ("complete", 0),
            ("complete", None),
            ("parts", {}),
            ("parts", "part-00000.csv"),
            ("source", []),
            ("source", "C:/data/catalog.xml"),
        ]
    )
)
def test_a_manifest_field_of_the_wrong_type_is_always_refused(
    field_and_value: tuple[str, object],
) -> None:
    """★ Constructed, not sampled: every wrong type in the table is refused.

    ★ The four ``type(...) is not int`` entries are the ones worth naming. ``bool`` is a
    subclass of ``int`` in Python, so ``isinstance(True, int)`` is True; and ``True == 1``
    and ``1.0 == 1`` are both true, so a value comparison accepts a boolean and a float.
    Those two mistakes are the exact defect M2 removed from this reader, and asserting
    them by value -- rather than trusting that a type check is still there -- is the only
    way they cannot come back.
    """
    field, value = field_and_value
    payload = manifest()
    payload[field] = value
    try:
        _read_from(payload)
    except CheckpointError:
        return
    raise AssertionError(f"manifest field {field!r} = {value!r} was accepted")


@PROPERTY_SETTINGS
@given(st.integers(min_value=0, max_value=1000), st.integers(min_value=1001, max_value=2000))
def test_rejecting_more_records_than_were_read_is_always_refused(
    records_consumed: int, rejected: int
) -> None:
    """Nothing checked this before M2, so a manifest could claim any number at all."""
    payload = manifest(records_consumed=records_consumed, rejected=rejected)
    try:
        _read_from(payload)
    except CheckpointError:
        return
    raise AssertionError(
        f"rejected={rejected} with records_consumed={records_consumed} was accepted"
    )


@PROPERTY_SETTINGS
@given(st.sampled_from(["part.csv", "part-1.csv", "part-00000.txt", "part-00000", "../x.csv"]))
def test_a_part_name_that_is_not_a_part_filename_is_always_refused(name: str) -> None:
    """Part names are joined onto the checkpoint's directory, so their shape is a boundary.

    This is the one place where a manifest names a path rather than a count, and the
    pattern is what stops ``parts[0].name`` from being ``../../something``.
    """
    payload = manifest(parts=[{"name": name, "rows": 1}])
    try:
        _read_from(payload)
    except CheckpointError:
        return
    raise AssertionError(f"part name {name!r} was accepted")


@PROPERTY_SETTINGS
@given(st.sampled_from(["path", "size", "sha256"]))
def test_a_source_block_missing_any_of_its_three_keys_is_always_refused(missing: str) -> None:
    """All three are required, so a manifest missing one cannot be compared against
    anything -- and refusing here keeps it from masquerading as a changed file."""
    source = {key: value for key, value in SOURCE.items() if key != missing}
    payload = manifest(source=source)
    try:
        _read_from(payload)
    except CheckpointError:
        return
    raise AssertionError(f"a source block without {missing!r} was accepted")


def _read_from(payload: dict[str, object]) -> Checkpoint:
    """Validate a manifest through the real public reader, in a temporary directory.

    The reader takes a path, so every constructed refusal below goes through a real file
    rather than through one of the private validators. That is deliberate: testing the
    validators would prove they work, and what needs proving is that the *entry point a
    resume actually calls* refuses.

    ``tempfile`` rather than ``tmp_path`` because these properties are not given a
    ``tmp_path``: a function-scoped fixture combined with ``@given`` is the one pairing
    hypothesis warns about, and criterion E's rule -- never write into the repository --
    is easier to keep with an explicit directory that cleans itself up.
    """
    with tempfile.TemporaryDirectory() as raw:
        real = pathlib.Path(raw) / "constructed.ckpt.json"
        real.write_text(json.dumps(payload), encoding="utf-8")
        return read_checkpoint(real)


# --- a gap this milestone found, pinned and NOT fixed --------------------------------


@FILE_PROPERTY_SETTINGS
@given(st.integers(min_value=1, max_value=2**31))
def test_the_writer_will_emit_a_manifest_its_own_reader_refuses(rejected: int) -> None:
    """★ ★ A real asymmetry, found by the round trip above and deliberately left unfixed.

    ``write_checkpoint`` serialises whatever :class:`Checkpoint` it is handed, without
    checking it against what ``read_checkpoint`` demands. A ``Checkpoint`` claiming more
    rejections than records read is unreachable from the CLI -- no run can produce one --
    so it is not a user-facing defect today. But the two halves of the same module do not
    agree on what a manifest is, and the first version of the round-trip property found it
    by failing.

    **This is criterion D's case and it is left as it is found.** M2's rule that a run
    cannot reject more records than it read is a *reader* rule; making the writer enforce
    it too is a change to an implementation, and implementation changes get their own
    review. The failure below is therefore recorded rather than removed: if the writer is
    tightened later, this test goes red and the report entry can be closed with evidence
    instead of a memory.
    """
    checkpoint = Checkpoint(
        source=dict(SOURCE),
        config=SHA256,
        records_consumed=0,
        rejected=rejected,
        parts=(),
        complete=False,
        version=CHECKPOINT_VERSION,
    )
    with tempfile.TemporaryDirectory() as raw:
        target = pathlib.Path(raw) / "impossible.ckpt.json"
        write_checkpoint(target, checkpoint)  # writes without complaint
        assert target.exists(), "the writer did emit it"
        try:
            read_checkpoint(target)
        except CheckpointError as exc:
            assert "cannot reject more records than it read" in str(exc)
            return
    raise AssertionError(
        "the reader now accepts a manifest the writer can emit; close the M12 gap entry"
    )
