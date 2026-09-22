"""The field table's state, and the one place it is judged.

**This module contains no validation of its own, and that is its whole point.** The brief
forbids a second implementation of the path, type and name rules inside ``gigaxml.gui``,
and the way to obey that is not to write a careful copy -- it is to have nothing to copy.
Every rule here is answered by :func:`gigaxml.config.parse_config`, and the answer shown to
the user is that function's own message, not a paraphrase of it.

The three exception types a bad config can raise -- :class:`ConfigError`,
:class:`RecordPathError`, :class:`FieldPathError` -- all derive from
:class:`~gigaxml.errors.GigaXMLError`, so one ``except`` covers them and none is quietly
turned into "something went wrong".

**Where a row is located.** When the whole-table config is rejected, the failing row is
found by validating each row on its own and seeing which one raises. That is a second call
into the same function, not a second implementation of it: the message shown is still the
library's, and no message is ever parsed to work out what went wrong.

**One rule the library cannot speak.** ``parse_config`` takes ``fields`` as a *mapping*, and
a mapping cannot hold two entries under the same name. Two rows wanting the same field name
therefore collapse before the library ever sees them. That collapse is noticed here and
reported; it is the only judgement this module makes on its own, and it is a fact about the
data structure rather than a re-statement of a rule that already exists.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from gigaxml.config import ExtractionConfig, parse_config
from gigaxml.errors import GigaXMLError
from gigaxml.fields import FieldType

__all__ = [
    "FIELD_TYPE_NAMES",
    "FieldRow",
    "ValidationResult",
    "build_config_dict",
    "validate",
]

#: What the type dropdown offers. **Derived from the enum**, so a seventh type added to
#: :class:`~gigaxml.fields.FieldType` appears in the interface without anyone remembering
#: to add it -- and, more to the point, a type the library does not know can never be
#: offered, because this list cannot contain one.
FIELD_TYPE_NAMES: tuple[str, ...] = tuple(member.value for member in FieldType)

#: The type a new row starts as. Matches what ``parse_config`` assumes when ``type`` is
#: absent, so a row the user has not touched validates exactly as an unwritten one would.
DEFAULT_TYPE_NAME = FieldType.STRING.value

#: A field that is certainly acceptable, used to tell "this row is wrong" from "the record
#: path is wrong". Named so it cannot collide with anything a user would type.
_PROBE_NAME = "__gigaxml_probe__"
_PROBE_PATH = "@__gigaxml_probe__"


@dataclass(frozen=True)
class FieldRow:
    """One row of the field table, as the user has it."""

    name: str
    path: str
    type_name: str = DEFAULT_TYPE_NAME
    required: bool = False

    def definition(self) -> dict[str, object]:
        """This row as a field definition, in the shape ``parse_config`` expects.

        ``type`` is always written out, even when it is the default. The interface shows
        a type for every row, so the config the panel validates is the config the user can
        see; leaving it implicit would make the two disagree the moment the default moved.
        """
        return {"path": self.path, "type": self.type_name, "required": self.required}


@dataclass(frozen=True)
class ValidationResult:
    """What ``parse_config`` said about the table.

    ``row_index`` is the row to mark when the failure belongs to one, and ``None`` when it
    does not -- a bad record path or a bad ``on_error`` is not any row's fault. An index
    rather than a name, because a row with an empty name is a failure worth pointing at and
    an empty string cannot be told from "no row".
    """

    ok: bool
    message: str
    row_index: int | None
    config: ExtractionConfig | None


def build_config_dict(
    rows: Sequence[FieldRow],
    *,
    record_path: str,
    namespaces: Mapping[str, str] | None = None,
    on_error: str | None = None,
) -> dict[str, object]:
    """The table as a config mapping, ready for ``parse_config``.

    Nothing is checked here. The mapping is assembled in the order the rows are in, so the
    validated config's ``fields`` come out in the order the user arranged them.
    """
    data: dict[str, object] = {"record": record_path}
    if namespaces:
        data["namespaces"] = dict(namespaces)
    data["fields"] = {row.name: row.definition() for row in rows}
    if on_error is not None:
        data["on_error"] = on_error
    return data


def validate(
    rows: Sequence[FieldRow],
    *,
    record_path: str,
    namespaces: Mapping[str, str] | None = None,
    on_error: str | None = None,
    source: str = "<the field table>",
) -> ValidationResult:
    """Ask the project whether this table is a config it would accept.

    Args:
        rows: the table, top to bottom.
        record_path: the record path the fields are relative to.
        namespaces: prefix-to-URI map, normally the document's own.
        on_error: the policy name, or ``None`` to leave the key out.
        source: the label the messages open with, so the user sees which table is wrong.

    Returns:
        The verdict, carrying the library's message verbatim when it is a rejection.
    """
    if not rows:
        # A table with no rows is a config with no fields, which the library rejects with
        # "is empty; nothing would be extracted". Saying so here would be inventing
        # wording, so the mapping is built with an empty fields block and handed over.
        pass

    data = build_config_dict(
        rows, record_path=record_path, namespaces=namespaces, on_error=on_error
    )

    collapsed = len(data["fields"])  # type: ignore[arg-type]
    if collapsed != len(rows):
        repeated = _duplicated_names(rows)
        return ValidationResult(
            ok=False,
            message=(
                f"two fields cannot share a name, and these do: {', '.join(repeated)}. "
                f"A config holds fields under their names, so the second would replace "
                f"the first."
            ),
            row_index=_duplicate_index(rows, repeated[0]) if repeated else None,
            config=None,
        )

    try:
        config = parse_config(data, source=source)
    except GigaXMLError as exc:
        return ValidationResult(
            ok=False,
            message=str(exc),
            row_index=_blame_row(rows, record_path, namespaces, on_error, source),
            config=None,
        )
    return ValidationResult(ok=True, message="", row_index=None, config=config)


def _duplicate_index(rows: Sequence[FieldRow], name: str) -> int:
    """The index of the *second* row using ``name``.

    The second, not the first: the first is the one that would be kept, and marking it
    would point the user at the row they do not have to change.
    """
    seen = False
    for index, row in enumerate(rows):
        if row.name == name:
            if seen:
                return index
            seen = True
    return 0


def _duplicated_names(rows: Iterable[FieldRow]) -> list[str]:
    """Names used by more than one row, in the order they first appear twice."""
    seen: set[str] = set()
    repeated: list[str] = []
    for row in rows:
        if row.name in seen and row.name not in repeated:
            repeated.append(row.name)
        seen.add(row.name)
    return repeated


def _shared_part_is_broken(
    record_path: str,
    namespaces: Mapping[str, str] | None,
    on_error: str | None,
    source: str,
) -> bool:
    """Whether a known-good row is rejected too.

    One row whose path and type are certainly acceptable, run through the same call. If
    that is refused, the fault is in ``record``, ``namespaces`` or ``on_error`` -- the
    parts every row shares -- and no row is to blame.

    Asking the library this question is the alternative to guessing from the failure
    pattern. An earlier version assumed "every row failed, so it must be the shared part",
    which blamed nothing at all for a one-row table whose only row was genuinely wrong.
    """
    probe = FieldRow(name=_PROBE_NAME, path=_PROBE_PATH)
    try:
        parse_config(
            build_config_dict(
                [probe],
                record_path=record_path,
                namespaces=namespaces,
                on_error=on_error,
            ),
            source=source,
        )
    except GigaXMLError:
        return True
    return False


def _blame_row(
    rows: Sequence[FieldRow],
    record_path: str,
    namespaces: Mapping[str, str] | None,
    on_error: str | None,
    source: str,
) -> int | None:
    """Which row the rejection came from, or ``None`` if it came from somewhere else.

    Found by asking the same question one row at a time. **The message is never parsed** --
    reading "field 'qty'" out of the text would break the first time somebody improved the
    sentence, which is the mistake this whole module exists to avoid.
    """
    if _shared_part_is_broken(record_path, namespaces, on_error, source):
        return None
    for index, row in enumerate(rows):
        try:
            parse_config(
                build_config_dict(
                    [row],
                    record_path=record_path,
                    namespaces=namespaces,
                    on_error=on_error,
                ),
                source=source,
            )
        except GigaXMLError:
            return index
    return None
