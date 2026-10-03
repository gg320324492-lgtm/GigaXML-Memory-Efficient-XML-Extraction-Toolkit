"""gigaxml — memory-efficient XML extraction toolkit.

**The public API is :data:`__all__`, and everything not named there is internal.**

That sentence is the whole of this milestone. Stage 3 spent M7, M8 and M9 splitting the
CLI, the inspection walk and the GUI into packages -- three refactors that move things
between modules, and therefore three chances to break somebody's import for no reason. A
rule that says "anything not documented may change in a minor release" is what makes those
refactors safe to ship; a rule that says nothing is what makes them dangerous. The full
statement, with every name and its tier, is ``python-api.md`` at the repository root.

**Three tiers, and the middle one is the point.**

* **Stable** -- promised across minor versions. Everything here a caller needs to do the
  job: load a config, read records, catch errors.
* **Beta** -- importable, documented, runnable examples included, **not** promised. The
  shape of these calls follows the internals, so it can move with them in a way the stable
  names cannot.
* **Internal** -- everything else, including the whole of :mod:`gigaxml.cli`,
  :mod:`gigaxml.inspect`, :mod:`gigaxml.gui`, :mod:`gigaxml.checkpoint` and
  :mod:`gigaxml.inspection`. **Their internal layout is not part of any contract**, which is
  precisely what licensed M7--M9.

**Why importing this module is cheap, and why that needed deciding.** Measured on this
machine: ``import gigaxml.errors`` costs 2.0 ms and loads no third-party module, while the
pipeline modules cost 30--54 ms each and pull in ``lxml`` (and ``yaml``, for configs). So
the error classes are imported here directly -- a caller whose only interest is
``except GigaXMLError`` should not pay for a YAML parser -- and the ten pipeline names are
resolved on first use through :pep:`562`'s module ``__getattr__``.

★ **This laziness is measured, not tidiness for its own sake.** Eagerly re-exporting
everything would make ``import gigaxml`` cost **54.5 ms instead of 1.3 ms** and load
``lxml`` and ``yaml`` for a caller who only wanted ``gigaxml.__version__``. The lazy form
costs about twenty lines and one ``TYPE_CHECKING`` block, and :mod:`tests.unit.test_public_api`
pins both halves of the trade -- the names still import, and the bare import still stays
cheap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

#: The error classes. Eagerly imported, and deliberately so: they are pure Python with no
#: dependency behind them (measured: 2.0 ms, nothing third-party), and they are the one
#: import a library user is most likely to make -- ``except GigaXMLError`` is how M6's
#: user-catchable boundary is meant to be used.
from gigaxml.errors import (
    CheckpointError,
    ConfigError,
    FieldPathError,
    FieldTypeError,
    GigaXMLError,
    InspectionError,
    InternalError,
    MissingRequiredFieldError,
    RecordPathError,
    RunInterruptedError,
    SchemaError,
    SecurityError,
    WriterError,
)

__version__ = "2.0.0rc4"

if TYPE_CHECKING:  # pragma: no cover - seen by type checkers, not at run time
    # ★ **The re-exports are declared twice on purpose, and the run-time half is the dict
    # below.** One spelling for both was not available -- an ``if TYPE_CHECKING`` block and an
    # eager import are the same statement, and the eager one is what costs 54 ms at import.
    # ``tests.unit.test_public_api`` asserts the two halves agree, so a name added to one and
    # not the other fails rather than shipping untyped.
    from gigaxml.config import ExtractionConfig, load_config, parse_config
    from gigaxml.fields import (
        ExtractionResult,
        FieldConfig,
        FieldType,
        extract_record,
    )
    from gigaxml.parser.streaming import StreamingRecordReader
    from gigaxml.writers import RowWriter, create_writer

#: Where each lazily-resolved name lives, as ``name -> "module: attribute"``.
#:
#: Spelled out rather than derived from the submodules, for the same reason the import list
#: above is: a declaration that can be read is a contract, and a declaration that has to be
#: executed to be understood is a mechanism. ``test_public_api.py`` asserts this dict and
#: :data:`__all__` and the ``TYPE_CHECKING`` block all name the same set, so none of the
#: three can drift without a failure.
_LAZY_EXPORTS: Final[dict[str, str]] = {
    "ExtractionConfig": "gigaxml.config:ExtractionConfig",
    "ExtractionResult": "gigaxml.fields:ExtractionResult",
    "FieldConfig": "gigaxml.fields:FieldConfig",
    "FieldType": "gigaxml.fields:FieldType",
    "RowWriter": "gigaxml.writers:RowWriter",
    "StreamingRecordReader": "gigaxml.parser.streaming:StreamingRecordReader",
    "create_writer": "gigaxml.writers:create_writer",
    "extract_record": "gigaxml.fields:extract_record",
    "load_config": "gigaxml.config:load_config",
    "parse_config": "gigaxml.config:parse_config",
}

#: The public API, grouped by tier rather than sorted.
#:
#: ★ **The grouping is the point, and it is why this one list opts out of ``RUF022``.**
#: Elsewhere the order carries no meaning, so those lists are sorted; here it carries the
#: one fact the file cannot state any other way -- which names are promised across a minor
#: version -- and a reader left to get that from ``python-api.md`` has been handed the work
#: this list exists to do. Suppressed on this list only.
__all__ = [  # noqa: RUF022 -- tier order is meaningful here; see the note above
    # -- stable ---------------------------------------------------------------
    "CheckpointError",
    "ConfigError",
    "ExtractionConfig",
    "FieldConfig",
    "FieldPathError",
    "FieldType",
    "FieldTypeError",
    "GigaXMLError",
    "InspectionError",
    "InternalError",
    "MissingRequiredFieldError",
    "RecordPathError",
    "RunInterruptedError",
    "SchemaError",
    "SecurityError",
    "StreamingRecordReader",
    "WriterError",
    "load_config",
    "parse_config",
    # -- beta -----------------------------------------------------------------
    "ExtractionResult",
    "RowWriter",
    "create_writer",
    "extract_record",
    # -- the version, which ``from gigaxml import *`` has always carried -------
    "__version__",
]


def __getattr__(name: str) -> object:
    """Resolve one of the lazily-exported names, or say plainly that there is no such thing.

    **A missing name raises :class:`AttributeError`, and nothing else.** That is not a
    detail: ``hasattr``, ``getattr(obj, name, default)`` and every ``except
    AttributeError`` in the ecosystem depend on it, and a custom exception type here would
    turn a typo into an unhandled crash somewhere unrelated.

    The message is a sentence rather than the bare default, because a bare
    ``AttributeError: name 'streaminReader'`` from a module that does nothing but re-export
    is the least helpful failure this package could produce. It says what *is* available
    and points at the document, which is what the reader needs next.
    """
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        near = sorted(_near_matches(name, __all__))
        hint = f" Did you mean: {', '.join(near)}?" if near else ""
        raise AttributeError(
            f"module 'gigaxml' has no attribute {name!r}.{hint} "
            f"The public API is listed in __all__ and documented in python-api.md; "
            "anything not named there is internal and may change in a minor release."
        )

    import importlib

    module_name, _, attribute = target.partition(":")
    value = getattr(importlib.import_module(module_name), attribute)
    # Cached on the module so the second access does not pay the import again, and so
    # ``dir(gigaxml)`` and a later attribute lookup agree with each other.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Every name a caller can reach, so ``dir(gigaxml)`` is not the twelve it was.

    Without this the lazy names are invisible to ``dir()``, to an interactive session and
    to anything that enumerates rather than imports -- which would make the laziness cost
    more than it saves.
    """
    return sorted(set(__all__) | set(_LAZY_EXPORTS))


def _near_matches(name: str, candidates: object) -> list[str]:
    """Names close enough to suggest, by :mod:`difflib`'s own measure.

    A cutoff of 0.6 rather than a hand-written rule: ``difflib`` already decided what
    "close" means, and a second opinion about it would only be a second thing to keep
    tuned.
    """
    import difflib

    return difflib.get_close_matches(name, list(candidates), n=3, cutoff=0.6)
