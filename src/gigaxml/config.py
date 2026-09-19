"""The YAML extraction config: which records to read, and how to type them.

A config has three top-level keys::

    record: /catalog/products/product      # or //products/product
    namespaces:
      c: "urn:example:catalog"
    fields:
      product_id:
        path: "@id"
        type: string
      price:
        path: "Price"
        type: decimal
        required: true

**Unknown keys are a hard error at every level.** The tempting alternative --
ignore what you do not recognise -- turns a typo into silent data corruption.
``type: deciaml`` would fall back to the default and emit the price as a string;
``requried: true`` would quietly stop enforcing a field. Both produce a
plausible-looking output file with wrong contents, which is the worst possible
failure mode for an extraction tool. A run that refuses to start is strictly
better.

Parsing is deliberately not schema-library driven: the project's only runtime
dependencies are ``lxml`` and ``pyyaml``, and a validator for three keys does not
justify a third one.

The ``record`` path is parsed by
:func:`gigaxml.parser.streaming.parse_record_path`, so its semantics -- root
anchoring, the ``//`` any-ancestor prefix, per-segment namespace resolution --
are exactly Phase 1.6's and cannot drift. ``namespaces`` applies to both the
record path and every field path.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final

import yaml

from gigaxml.errors import ConfigError
from gigaxml.fields import FieldConfig, FieldType, parse_field_path
from gigaxml.parser.streaming import RecordPathSpec, parse_record_path

__all__ = [
    "ExtractionConfig",
    "load_config",
    "parse_config",
]

#: Keys allowed at the top level of a config document.
_TOP_LEVEL_KEYS: Final = frozenset({"record", "namespaces", "fields", "on_error"})

#: Keys allowed inside one entry of ``fields``.
_FIELD_KEYS: Final = frozenset({"path", "type", "required"})

#: The policy a config gets when it does not declare one.
_DEFAULT_ERROR_POLICY: Final = "abort"

#: The type a field gets when it does not declare one.
_DEFAULT_FIELD_TYPE: Final = FieldType.STRING

#: Supported type names, for error messages.
_TYPE_NAMES: Final = {field_type.value: field_type for field_type in FieldType}


class ErrorPolicy(Enum):
    """What a run does when one record cannot be extracted.

    ``ABORT`` is the default, and is the behaviour this tool has always had: the
    first unconvertible value or missing required field ends the run, with a
    non-zero exit code and whatever rows had already been flushed left on disk.

    ``QUARANTINE`` is the opt-in alternative for the case the default is worst at --
    a multi-gigabyte file where a handful of records are malformed. It writes each
    rejected record to a rejection log, keeps going, and exits 0; the count and the
    log's path go into the run report. It is deliberately **not** the default:
    quietly skipping records is a way to produce a file that looks complete and is
    missing rows, and that decision belongs to whoever asked for the extraction.
    """

    ABORT = "abort"
    QUARANTINE = "quarantine"

    @classmethod
    def from_name(cls, name: str) -> ErrorPolicy:
        """Look up a policy by its config name.

        Raises:
            ConfigError: ``name`` is not a known policy.
        """
        try:
            return cls(name.strip().lower())
        except ValueError as exc:
            known = sorted(member.value for member in cls)
            raise ConfigError(
                f"unsupported on_error value {name!r}; supported values are {known}"
            ) from exc


@dataclass(frozen=True, slots=True)
class ExtractionConfig:
    """A validated extraction config.

    Attributes:
        record_path: the record path exactly as written.
        record_spec: its parsed form, including the anchoring mode.
        namespaces: prefix-to-URI map; the empty string key is the default
            namespace. Shared by the record path and every field path.
        fields: the validated field definitions, in config order.
        on_error: what to do when one record cannot be extracted. Defaults to
            :attr:`ErrorPolicy.ABORT`.
    """

    record_path: str
    record_spec: RecordPathSpec
    namespaces: dict[str, str]
    fields: tuple[FieldConfig, ...]
    on_error: ErrorPolicy = ErrorPolicy.ABORT

    @property
    def field_names(self) -> tuple[str, ...]:
        """The configured field names, in config order."""
        return tuple(field.name for field in self.fields)


def load_config(path: str | Path) -> ExtractionConfig:
    """Read and validate a YAML config file.

    Args:
        path: the ``.yaml``/``.yml`` file to read.

    Returns:
        The validated config.

    Raises:
        ConfigError: the file cannot be read, is not valid YAML, or does not
            satisfy :func:`parse_config`.
    """
    location = Path(path)
    try:
        # utf-8-sig, not utf-8: a byte-order mark would otherwise survive as
        # U+FEFF and turn the first key into "\ufeffrecord", producing a baffling
        # "unknown key" error on a file that looks perfectly fine.
        text = location.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {str(location)!r}: {exc}") from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"config file {str(location)!r} is not valid YAML: {exc}") from exc

    if data is None:
        raise ConfigError(f"config file {str(location)!r} is empty")

    return parse_config(data, source=str(location))


def parse_config(data: object, *, source: str = "<config>") -> ExtractionConfig:
    """Validate an already-loaded config mapping.

    Args:
        data: the decoded YAML document, normally a ``dict``.
        source: a label used in error messages, normally the file path.

    Returns:
        The validated config.

    Raises:
        ConfigError: an unknown key, a missing or mistyped ``record``/``fields``,
            a field without ``path``, an unsupported ``type`` name, a
            non-boolean ``required``, a malformed ``namespaces`` block, or an
            ``on_error`` value that is not a supported policy.
        gigaxml.errors.RecordPathError: ``record`` is not a valid record path.
            Propagated rather than re-wrapped: it already names the offending
            path and explains anchoring, and wrapping it would only bury that.
        gigaxml.errors.FieldPathError: a field ``path`` is not a valid relative
            path. Propagated for the same reason.
    """
    if not isinstance(data, Mapping):
        raise ConfigError(
            f"{source} must contain a mapping at the top level, got {type(data).__name__}"
        )

    _reject_unknown_keys(data.keys(), _TOP_LEVEL_KEYS, "the top level", source)

    record_path = data.get("record")
    if not isinstance(record_path, str) or not record_path.strip():
        raise ConfigError(
            f"{source} must set 'record' to a non-empty element path string, got {record_path!r}"
        )
    record_path = record_path.strip()

    namespaces = _parse_namespaces(data.get("namespaces"), source)
    fields = _parse_fields(data.get("fields"), namespaces, source)
    on_error = _parse_error_policy(data.get("on_error"), source)

    return ExtractionConfig(
        record_path=record_path,
        record_spec=parse_record_path(record_path, namespaces or None),
        namespaces=namespaces,
        fields=fields,
        on_error=on_error,
    )


def _parse_error_policy(raw: object, source: str) -> ErrorPolicy:
    """Validate the optional ``on_error`` key into a policy."""
    if raw is None:
        return ErrorPolicy(_DEFAULT_ERROR_POLICY)
    if not isinstance(raw, str):
        raise ConfigError(
            f"{source} must set 'on_error' to a string, got {type(raw).__name__}; "
            f"supported values are {sorted(member.value for member in ErrorPolicy)}"
        )
    return ErrorPolicy.from_name(raw)


def _reject_unknown_keys(
    keys: Iterable[object], allowed: frozenset[str], where: str, source: str
) -> None:
    """Fail on any key that is not in ``allowed``.

    See the module docstring: silently ignoring an unrecognised key is how a
    typo becomes wrong data instead of a failed run.
    """
    unknown = sorted(str(key) for key in keys if key not in allowed)
    if unknown:
        raise ConfigError(
            f"unknown key(s) {unknown} in {where} of {source}; allowed keys are {sorted(allowed)}"
        )


def _parse_namespaces(raw: object, source: str) -> dict[str, str]:
    """Validate the optional ``namespaces`` block into a prefix-to-URI map."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ConfigError(
            f"'namespaces' in {source} must be a mapping of prefix to URI, got {type(raw).__name__}"
        )

    namespaces: dict[str, str] = {}
    for prefix, uri in raw.items():
        if not isinstance(prefix, str):
            raise ConfigError(f"namespace prefix {prefix!r} in {source} must be a string")
        if not isinstance(uri, str) or not uri:
            raise ConfigError(
                f"namespace prefix {prefix!r} in {source} must map to a non-empty URI "
                f"string, got {uri!r}"
            )
        namespaces[prefix] = uri
    return namespaces


def _parse_fields(
    raw: object,
    namespaces: Mapping[str, str],
    source: str,
) -> tuple[FieldConfig, ...]:
    """Validate the ``fields`` block into field definitions, in config order."""
    if raw is None:
        raise ConfigError(f"{source} must define 'fields'")
    if not isinstance(raw, Mapping):
        raise ConfigError(
            f"'fields' in {source} must be a mapping of field name to definition, "
            f"got {type(raw).__name__}"
        )
    if not raw:
        raise ConfigError(f"'fields' in {source} is empty; nothing would be extracted")

    fields: list[FieldConfig] = []
    for name, definition in raw.items():
        if not isinstance(name, str) or not name:
            raise ConfigError(f"field name {name!r} in {source} must be a non-empty string")
        fields.append(_parse_field(name, definition, namespaces, source))
    return tuple(fields)


def _parse_field(
    name: str,
    definition: object,
    namespaces: Mapping[str, str],
    source: str,
) -> FieldConfig:
    """Validate one entry of ``fields``."""
    where = f"field {name!r}"
    if not isinstance(definition, Mapping):
        raise ConfigError(
            f"{where} in {source} must be a mapping with at least a 'path', "
            f"got {type(definition).__name__}"
        )
    _reject_unknown_keys(definition.keys(), _FIELD_KEYS, where, source)

    raw_path = definition.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ConfigError(f"{where} in {source} must set 'path' to a non-empty string")

    raw_type = definition.get("type")
    if raw_type is None:
        field_type = _DEFAULT_FIELD_TYPE
    elif isinstance(raw_type, str) and raw_type in _TYPE_NAMES:
        field_type = _TYPE_NAMES[raw_type]
    else:
        raise ConfigError(
            f"{where} in {source} declares unsupported type {raw_type!r}; "
            f"supported types are {sorted(_TYPE_NAMES)}"
        )

    required = definition.get("required", False)
    if not isinstance(required, bool):
        raise ConfigError(
            f"{where} in {source} must set 'required' to true or false, got {required!r}"
        )

    return FieldConfig(
        name=name,
        raw_path=raw_path.strip(),
        spec=parse_field_path(raw_path, namespaces or None),
        type=field_type,
        required=required,
    )
