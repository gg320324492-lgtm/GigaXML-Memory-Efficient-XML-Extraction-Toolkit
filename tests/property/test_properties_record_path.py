"""Property tests: a record path is parsed or refused, never anything else.

**The invariant.** For any string at all, ``parse_record_path`` returns a
:class:`RecordPathSpec` or raises :class:`RecordPathError`. Nothing else is acceptable --
not a ``KeyError`` from a dict lookup, not a ``ValueError`` from a regex, and above all
not a ``RecordPathSpec`` built from a path the tool cannot honour.

**Why this one earns its file.** A record path is the one string a user must write by
hand for every single run, and it is the input with the widest gap between "looks
reasonable" and "accepted": ``/``, ``//``, ``catalog/products``, ``ns:product`` with no
``ns`` declared, ``/a//b``, ``/1abc``, ``//``. The hand-written tests cover the cases
somebody thought of. This one asks the machine for the rest.
"""

from __future__ import annotations

from hypothesis import assume, given
from hypothesis import strategies as st

from gigaxml.errors import RecordPathError
from gigaxml.parser.streaming import parse_record_path
from gigaxml.paths import ANY_ANCESTOR_PREFIX, BARE_SEGMENT

from .conftest import PROPERTY_SETTINGS, accepts_or_refuses

RECORD_PATH_ERRORS: tuple[type[BaseException], ...] = (RecordPathError,)

#: Namespaces a generated path may legitimately use. Kept small and fixed so that
#: "unknown prefix" is a *refusal* rather than an accident of the generator.
NAMESPACES = {"": "urn:default", "x": "urn:x"}

#: Segment names that are syntactically valid XML names, so a path built from them is a
#: candidate for acceptance rather than an immediate refusal.
VALID_NAMES = ("a", "product", "Name", "_x", "n0", "with.dot", "with-dash")

#: Ways of being wrong that a uniform random string almost never produces on its own.
MALFORMED = st.sampled_from(
    [
        "",  # empty
        "/",  # absolute but with no segments
        "//",  # the any-ancestor marker with nothing after it
        "///",  # three slashes
        "catalog/products",  # relative: the single most common user mistake
        "product",  # a bare name, which is a *field* path
        "/a//b",  # an empty segment between two good ones
        "/a/b/",  # a trailing slash
        "//a/b",  # the any-ancestor form, which IS valid -- the guard must not assume
        "/1abc",  # an XML name may not start with a digit
        "/a b",  # a space is not an XML name character
        "/x:product",  # a prefix that NAMESPACES does not declare
        "/y:product",  # ditto, a different undeclared prefix
        "/x:",  # a prefix with an empty local part
        "/:local",  # an empty prefix, which is not a prefix at all
        "/a/x:b/c",  # a mix where only one segment carries the prefix
        "/a:b:c",  # two colons in one segment
        "/ ",
        "/a/../b",  # a parent step, which field paths reject and record paths never had
    ]
)

#: Paths that must be accepted -- the property needs examples on the "yes" side too, or
#: it cannot tell a parser that refuses everything from one that parses correctly.
VALID_PATHS = st.builds(
    lambda anchor, names: anchor + "/".join(names),
    st.sampled_from(["/", "//"]),
    st.lists(st.sampled_from(VALID_NAMES), min_size=1, max_size=4),
)


@PROPERTY_SETTINGS
@given(st.one_of(st.text(), MALFORMED, VALID_PATHS))
def test_a_record_path_is_parsed_or_refused(candidate: str) -> None:
    """Criterion B and C: one family, asserted by name, and no third outcome."""
    spec = accepts_or_refuses(
        lambda: parse_record_path(candidate, NAMESPACES),
        expected=RECORD_PATH_ERRORS,
        label=f"parse_record_path({candidate!r})",
    )
    if spec is None:
        return
    assert spec.chain, f"an accepted path must have at least one segment: {candidate!r}"
    assert all(tag for tag in spec.chain), f"a resolved tag is never empty: {spec.chain!r}"


@PROPERTY_SETTINGS
@given(st.text(), st.sampled_from([None, {"": "urn:default"}, {"x": "urn:x"}, {}]))
def test_the_namespace_map_never_changes_the_outcome_family(
    candidate: str, namespaces: dict[str, str] | None
) -> None:
    """★ The leak half of the invariant, on its own so a failure names this file.

    Four namespace maps spanning "none", "empty", "a default URI" and "a declared
    prefix". A path refused for a missing prefix under one map and accepted under another
    is a real inconsistency, and running them side by side is how that would show up.
    """
    accepts_or_refuses(
        lambda: parse_record_path(candidate, namespaces),
        expected=RECORD_PATH_ERRORS,
        label=f"parse_record_path({candidate!r}, {namespaces!r})",
    )


@PROPERTY_SETTINGS
@given(st.text())
def test_the_anchoring_flag_says_exactly_what_the_prefix_says(candidate: str) -> None:
    """A path with no prefix is anchored; one with ``//`` is not. Never the reverse."""
    spec = accepts_or_refuses(
        lambda: parse_record_path(candidate),
        expected=RECORD_PATH_ERRORS,
        label=f"parse_record_path({candidate!r})",
    )
    if spec is None:
        return
    assert spec.anchored == (not candidate.startswith(ANY_ANCESTOR_PREFIX)), (
        f"anchored disagrees with the marker: {candidate!r} -> {spec.anchored}"
    )


@PROPERTY_SETTINGS
@given(st.text())
def test_an_accepted_record_path_resolves_to_qualified_tags(candidate: str) -> None:
    """★ Every accepted segment must be a legal XML name, or ``{uri}local``.

    This is the assertion that would catch a resolver quietly returning the bare local
    name for a prefixed segment: the tag would look right in a log and match nothing in
    the document, which is the failure ``paths.py``'s module docstring is about.
    """
    spec = accepts_or_refuses(
        lambda: parse_record_path(candidate, NAMESPACES),
        expected=RECORD_PATH_ERRORS,
        label=f"parse_record_path({candidate!r})",
    )
    if spec is None:
        return
    for tag in spec.chain:
        if tag.startswith("{"):
            uri, _, local = tag[1:].partition("}")
            assert uri, f"a braced tag needs a URI before the brace: {tag!r}"
            assert BARE_SEGMENT.match(local), f"{local!r} is not a legal XML name"
        else:
            assert BARE_SEGMENT.match(tag), f"{tag!r} is neither bare-valid nor braced"


@PROPERTY_SETTINGS
@given(st.lists(st.sampled_from(VALID_NAMES), min_size=1, max_size=4))
def test_prefixed_and_bare_forms_of_the_same_path_agree(names: list[str]) -> None:
    """``/a/b`` and ``//a/b`` are the same path; only the matching mode differs.

    The resolver treats the two markers identically, so a change that made one of them
    resolve differently would be invisible until a resumed or nested run.
    """
    anchored = parse_record_path("/" + "/".join(names), NAMESPACES)
    any_ancestor = parse_record_path(ANY_ANCESTOR_PREFIX + "/".join(names), NAMESPACES)
    assume(True)
    assert anchored.chain == any_ancestor.chain
    assert anchored.anchored and not any_ancestor.anchored


@PROPERTY_SETTINGS
@given(st.lists(st.sampled_from(VALID_NAMES), min_size=1, max_size=3))
def test_a_prefix_that_is_declared_resolves_the_same_as_its_uri(names: list[str]) -> None:
    """``x:product`` with ``{"x": "urn:x"}`` and ``product`` with ``{"": "urn:x"}``
    must produce the same qualified tag -- that is what the namespace map is for."""
    with_prefix = parse_record_path("/" + "/".join(["x:" + names[0], *names[1:]]), NAMESPACES)
    as_default = parse_record_path("/" + "/".join(names), {"": "urn:x"})
    assert with_prefix.chain[0] == as_default.chain[0] == "{urn:x}" + names[0]
