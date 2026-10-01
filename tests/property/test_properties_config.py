"""Property tests: a config is accepted or refused, never anything else.

**The invariant.** For any mapping at all, ``parse_config`` returns an
:class:`ExtractionConfig` or raises one of this project's own error types.

★ **A deviation from the brief, measured rather than assumed.** The brief says a config is
accepted or raises ``ConfigError``. It does not: ``parse_config`` validates the ``record``
key by calling ``parse_record_path`` and a field's ``path`` by calling
``parse_field_path``, and both of those raise their own error type. The docstring says so
deliberately -- "wrapping it would only bury that" -- so the promised family here is three
types wide, not one. They are all :class:`GigaXMLError` subclasses, so the *invariant* the
milestone states holds exactly; only the enumeration had to be corrected.

★ **M2 is why this file is worth having.** That milestone replaced lenient coercion with
strict checking, and the tests written with it cover the cases its author thought of. The
properties below cover the ones nobody thought of, and the second half of the file asserts
the *specific* refusals M2 introduced -- a ``version`` that is accepted without being
understood, a ``required`` that is a truthy string, an unknown key -- by construction
rather than by example, so they hold for every value of those shapes rather than for the
two that were written down.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from gigaxml.config import ExtractionConfig, parse_config
from gigaxml.errors import ConfigError, FieldPathError, RecordPathError

from .conftest import PROPERTY_SETTINGS, accepts_or_refuses

#: Three types wide, and deliberately so -- see the module docstring.
CONFIG_ERRORS: tuple[type[BaseException], ...] = (ConfigError, RecordPathError, FieldPathError)

TOP_LEVEL_KEYS = frozenset({"record", "namespaces", "fields", "on_error", "schema", "version"})
FIELD_KEYS = frozenset({"path", "type", "required"})
TYPE_NAMES = ("string", "int", "float", "decimal", "bool", "date")

#: Field paths known to parse, so the property above varies the *type* name only.
VALID_NAMES = ("a", "product", "Name", "_x", "n0")

#: Any JSON-shaped value, so the generator reaches types a YAML file can actually carry.
JSON_VALUE = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**40), max_value=2**40)
    | st.floats(allow_nan=False, allow_infinity=False, width=32)
    | st.text(max_size=12),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=12,
)

#: A sentinel meaning "leave this key out", so a strategy can express absence
#: without a separate branch per key.
_ABSENT = object()

#: A config that is known good, used as the base for the "one key wrong" strategies.
GOOD_CONFIG = {
    "record": "/catalog/products/product",
    "fields": {"id": {"path": "@id"}, "name": {"path": "name"}},
}

#: A mapping drawn from the real key set but with arbitrary values -- the space where a
#: lenient parser would quietly produce a config.
NEAR_VALID = st.builds(
    lambda record, fields, version, on_error: {
        key: value
        for key, value in (
            ("record", record),
            ("fields", fields),
            ("version", version),
            ("on_error", on_error),
        )
        if value is not _ABSENT
    },
    st.one_of(
        st.sampled_from(["/a/b", "//a/b", "a/b", "/", "", "/a b", "/1x"]), st.none(), st.integers()
    ),
    st.one_of(
        st.just({"f": {"path": "p"}}),
        st.just({}),
        st.just({"f": None}),
        st.just({"f": {"path": "p", "type": "int"}}),
        st.just({"f": {"path": "p", "type": "nope"}}),
        st.just({"f": {"path": "p", "required": "yes"}}),
        st.just({"f": {"path": "/abs"}}),
        st.just({"f": {"path": "*"}}),
    ),
    st.one_of(st.just(_ABSENT), st.just(1), st.just(2), st.just(True), st.just(1.0), st.just("1")),
    st.one_of(st.just(_ABSENT), st.just("abort"), st.just("quarantine"), st.just("nope")),
)


@PROPERTY_SETTINGS
@given(
    st.dictionaries(
        # ★ Half the keys are real top-level keys. Uniform random keys almost never spell
        # "record", so the generator never reached `parse_record_path` and this property
        # never exercised the branch that lets `RecordPathError` through -- measured, when
        # mutation F1c stayed green against this test alone while going red against its
        # sibling. A property can be correct and still have a strategy too weak to reach
        # the code it claims to cover, and nothing warns you except a mutation that fails
        # to be caught.
        st.one_of(st.sampled_from(sorted(TOP_LEVEL_KEYS)), st.text(max_size=10)),
        JSON_VALUE,
        max_size=6,
    )
)
def test_an_arbitrary_config_is_accepted_or_refused_by_name(mapping: dict[str, object]) -> None:
    """Criterion B and C over the whole input space, not a curated corner of it."""
    config = accepts_or_refuses(
        lambda: parse_config(mapping),
        expected=CONFIG_ERRORS,
        label=f"parse_config({mapping!r})",
    )
    if config is None:
        return
    assert config.fields, "an accepted config always has at least one field"
    assert all(field.name for field in config.fields), (
        f"an accepted config has no nameless field: {config.fields!r}"
    )


@PROPERTY_SETTINGS
@given(NEAR_VALID)
def test_a_nearly_valid_config_is_accepted_or_refused_by_name(mapping: dict[str, object]) -> None:
    """★ The space the hand-written tests do not reach: valid keys, plausible values.

    Every key here is one the parser knows. So the only thing that can decide the outcome
    is the *value*, which is exactly where a lenient parser would quietly succeed.
    """
    config = accepts_or_refuses(
        lambda: parse_config(mapping),
        expected=CONFIG_ERRORS,
        label=f"parse_config({mapping!r})",
    )
    if config is None:
        return
    assert config.record_path.strip() == config.record_path
    assert config.fields


@PROPERTY_SETTINGS
@given(st.text(max_size=10).filter(lambda key: key not in TOP_LEVEL_KEYS), JSON_VALUE)
def test_an_unknown_top_level_key_is_always_refused(key: str, filler: object) -> None:
    """★ Constructed, not sampled: every unknown key is refused, whatever else it holds.

    ``True`` and ``1`` cannot be YAML keys in a way that matters here, but a key that
    happens to be a valid *value* type must not be mistaken for a valid key.
    """
    mapping = {"record": "/a/b", "fields": {"f": {"path": "p"}}, key: filler}
    accepts_or_refuses(
        lambda: parse_config(mapping),
        expected=CONFIG_ERRORS,
        label=f"parse_config with unknown key {key!r}",
    )
    try:
        parse_config(mapping)
    except (ConfigError, RecordPathError, FieldPathError) as exc:
        assert isinstance(exc, ConfigError), (
            f"an unknown top-level key must be a ConfigError naming the key, got "
            f"{type(exc).__name__}: {exc}"
        )
        return
    raise AssertionError(f"an unknown top-level key {key!r} was accepted")


@PROPERTY_SETTINGS
@given(st.text(max_size=8).filter(lambda key: key not in FIELD_KEYS), JSON_VALUE)
def test_an_unknown_field_key_is_always_refused(key: str, filler: object) -> None:
    """The same rule one level down, where a stray key is easier to add by accident."""
    mapping = {
        "record": "/a/b",
        "fields": {"f": {"path": "p", key: filler}},
    }
    try:
        parse_config(mapping)
    except (ConfigError, RecordPathError, FieldPathError) as exc:
        assert isinstance(exc, ConfigError), (
            f"an unknown field key must be a ConfigError, got {type(exc).__name__}: {exc}"
        )
        return
    raise AssertionError(f"an unknown field key {key!r} was accepted")


@PROPERTY_SETTINGS
@given(st.one_of(st.just(True), st.just(1.0), st.just(2), st.just("1"), st.just(None), st.just(-1)))
def test_a_version_this_build_does_not_read_is_always_refused(version: object) -> None:
    """★ M2's rule, asserted for every wrong shape rather than the two that were written.

    ``True == 1`` and ``1.0 == 1`` in Python, so a value comparison would admit both --
    the exact defect the manifest reader had. This property covers booleans, floats,
    strings, negatives and ``None`` in one go, so adding a new comparison style without
    the type check cannot pass.
    """
    mapping = {**GOOD_CONFIG, "version": version}
    try:
        parse_config(mapping)
    except (ConfigError, RecordPathError, FieldPathError) as exc:
        assert isinstance(exc, ConfigError)
        return
    raise AssertionError(f"version {version!r} was accepted but this build reads only 1")


@PROPERTY_SETTINGS
@given(st.one_of(st.just("yes"), st.just(1), st.just(0), st.just(None), st.just([])))
def test_a_required_flag_that_is_not_a_boolean_is_always_refused(required: object) -> None:
    """``"false"`` is a truthy string. A config that said it was not refused would hide
    every missing required field behind a flag the reader believes they set."""
    mapping = {"record": "/a/b", "fields": {"f": {"path": "p", "required": required}}}
    try:
        parse_config(mapping)
    except (ConfigError, RecordPathError, FieldPathError) as exc:
        assert isinstance(exc, ConfigError)
        return
    raise AssertionError(f"required: {required!r} was accepted but is not a boolean")


@PROPERTY_SETTINGS
@given(
    st.text(max_size=10).filter(lambda name: name not in TYPE_NAMES), st.sampled_from(VALID_NAMES)
)
def test_an_unsupported_type_name_is_always_refused(type_name: str, path: str) -> None:
    """An unknown ``type:`` must be refused rather than defaulting to string.

    Defaulting would produce a config that loads, runs, and writes every value as text --
    the shape of failure where the output looks fine and is wrong.
    """
    mapping = {"record": "/a/b", "fields": {"f": {"path": path, "type": type_name}}}
    try:
        parse_config(mapping)
    except (ConfigError, RecordPathError, FieldPathError) as exc:
        assert isinstance(exc, ConfigError)
        return
    raise AssertionError(f"type {type_name!r} was accepted but is not one of {TYPE_NAMES}")


@PROPERTY_SETTINGS
@given(
    st.sampled_from(
        [
            "a/b",
            "product",
            "/",
            "//",
            "/a b",
            "/1x",
            "catalog/products",
            "//product",
            "/a:",
            "/:a",
            "/a/b/../c",
        ]
    )
)
def test_a_bad_record_path_is_a_record_path_error_not_a_config_error(record: str) -> None:
    """★ Why the promised family is three types wide, asserted rather than assumed.

    ``parse_config`` validates ``record`` by calling ``parse_record_path`` and lets that
    error propagate, so a config whose record path is *malformed* is refused with
    :class:`RecordPathError` -- **not** :class:`ConfigError`.

    ★ **Written this way after the first version was wrong.** It also listed ``"  "``, and
    that value is refused with a ``ConfigError`` -- by ``not record_path.strip()`` a few
    lines earlier, before the path resolver is ever called. The distinction is real and
    worth stating rather than smoothing over: **a blank record is a config problem, a
    malformed one is a path problem**, and both belong to the promised family.

    ★ Constructed rather than generated, and the reason is worth recording: mutation F1c
    narrowed the promised family by dropping these two types and stayed **green** against
    the arbitrary-mapping property, because a uniform random mapping almost never spells
    ``record`` *and* gives it a string *and* supplies a usable ``fields`` block. The guard
    was correct; the strategy was too weak to reach the branch. This property is the fix,
    and it is a statement about behaviour rather than a tuning of a generator.
    """
    mapping = {"record": record, "fields": {"f": {"path": "p"}}}
    try:
        parse_config(mapping)
    except RecordPathError:
        return
    except (ConfigError, FieldPathError) as exc:
        raise AssertionError(
            f"a malformed record path must surface as RecordPathError, not "
            f"{type(exc).__name__}: {record!r} -> {exc}"
        ) from exc


@PROPERTY_SETTINGS
@given(st.sampled_from(["", " ", "  ", "\t", "\n", "   \t  "]))
def test_a_blank_record_is_a_config_error_not_a_path_error(blank: str) -> None:
    """The other half of the boundary above, pinned separately so neither can drift."""
    mapping = {"record": blank, "fields": {"f": {"path": "p"}}}
    try:
        parse_config(mapping)
    except ConfigError:
        return
    except (RecordPathError, FieldPathError) as exc:
        raise AssertionError(
            f"a blank record is a config problem and must be a ConfigError, not "
            f"{type(exc).__name__}: {blank!r} -> {exc}"
        ) from exc


@PROPERTY_SETTINGS
@given(st.sampled_from(["*", "a/*", "text()", "a[1]", "/abs", "..", "a/../b", "a//b", "@", "a/@b"]))
def test_a_bad_field_path_is_a_field_path_error_not_a_config_error(field_path: str) -> None:
    """The same statement for a field's ``path``, which is the third member of the family."""
    mapping = {"record": "/a/b", "fields": {"f": {"path": field_path}}}
    try:
        parse_config(mapping)
    except FieldPathError:
        return
    except (ConfigError, RecordPathError) as exc:
        raise AssertionError(
            f"a bad field path must surface as FieldPathError, not {type(exc).__name__}: "
            f"{field_path!r} -> {exc}"
        ) from exc


@PROPERTY_SETTINGS
@given(st.lists(st.sampled_from(TYPE_NAMES), min_size=1, max_size=4))
def test_an_accepted_config_round_trips_every_field_type(type_names: list[str]) -> None:
    """★ The end-to-end shape: what comes back has the fields that were asked for.

    ``ExtractionConfig.fields`` is what every downstream stage reads, and it is built by
    a different function than the one that validated the mapping. A field dropped, renamed
    or re-ordered here would be invisible to every test that checks only the parse.
    """
    fields = {f"f{index}": {"path": "p", "type": name} for index, name in enumerate(type_names)}
    config = parse_config({**GOOD_CONFIG, "fields": fields})
    assert isinstance(config, ExtractionConfig)
    assert [field.name for field in config.fields] == list(fields), (
        f"field order or names changed: {[f.name for f in config.fields]}"
    )
    assert [field.type.value for field in config.fields] == type_names
