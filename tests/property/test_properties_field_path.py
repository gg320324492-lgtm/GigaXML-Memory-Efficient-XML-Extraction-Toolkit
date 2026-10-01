"""Property tests: a field path is parsed or refused, never anything else.

**The invariant.** For any string at all, ``parse_field_path`` returns a
:class:`FieldPathSpec` or raises :class:`FieldPathError`.

★ **The field path is the input space where a silent wrong answer is most likely.** A
record path that resolves wrongly usually matches nothing and says so. A field path that
resolves wrongly can match the *wrong element* and produce a plausible-looking row, which
is the one outcome a user cannot detect by looking at the output. So this file leans on
properties that check the *shape* of an accepted path, not just that something came back.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from gigaxml.errors import FieldPathError
from gigaxml.fields import parse_field_path
from gigaxml.paths import BARE_SEGMENT

from .conftest import PROPERTY_SETTINGS, accepts_or_refuses

FIELD_PATH_ERRORS: tuple[type[BaseException], ...] = (FieldPathError,)

NAMESPACES = {"": "urn:default", "x": "urn:x"}

VALID_NAMES = ("a", "product", "Name", "_x", "n0", "with.dot", "with-dash")

#: The five forms ``parse_field_path``'s docstring promises, generated rather than listed,
#: so a change to the accepted grammar shows up as a shrinking example rather than as a
#: hand-written case somebody forgot to update.
FORMS = st.one_of(
    st.builds(lambda n: n, st.sampled_from(VALID_NAMES)),
    st.builds(lambda a: "@" + a, st.sampled_from(VALID_NAMES)),
    st.just("."),
    st.builds(lambda a, b: f"{a}/{b}", st.sampled_from(VALID_NAMES), st.sampled_from(VALID_NAMES)),
    st.builds(lambda e, a: f"{e}/@{a}", st.sampled_from(VALID_NAMES), st.sampled_from(VALID_NAMES)),
    st.builds(lambda p: f"x:{p}", st.sampled_from(VALID_NAMES)),
)

#: Ways of being wrong. Each entry is a shape a user can plausibly write down.
MALFORMED = st.sampled_from(
    [
        "",  # empty
        "   ",  # whitespace only
        "/Name",  # absolute: a record path, pasted into a field
        "//Name",
        "@",  # an attribute with no name
        "a/@",  # an attribute slot at the end with nothing in it
        "@a/b",  # an attribute that is not last
        "a/@b/c",  # ditto, further from the end
        "a//b",  # an empty segment
        "/a",  # leading slash
        "a/",  # trailing slash
        "..",  # a parent step
        "a/../b",
        "*",  # wildcard
        "a/*",
        "text()",  # an XPath function
        "a[1]",  # a predicate
        "a/@b[1]",
        "(a)",  # a parenthesised group
        "x:Name",  # valid prefix, but see the namespaces parameter
        ":Name",  # an empty prefix
        "x:",  # a prefix with an empty local part
        "1abc",  # an XML name may not start with a digit
        "a b",
        "@a@b",  # two attribute markers
    ]
)


@PROPERTY_SETTINGS
@given(st.one_of(st.text(), MALFORMED, FORMS))
def test_a_field_path_is_parsed_or_refused(candidate: str) -> None:
    """Criterion B and C: one family, asserted by name, and no third outcome."""
    accepts_or_refuses(
        lambda: parse_field_path(candidate, NAMESPACES),
        expected=FIELD_PATH_ERRORS,
        label=f"parse_field_path({candidate!r})",
    )


@PROPERTY_SETTINGS
@given(st.text())
def test_a_dot_is_always_the_whole_record(candidate: str) -> None:
    """``.`` is the one path that means "the record element itself", and nothing else is.

    It must never pick up an attribute or a segment from anywhere, because a field mapped
    to ``.`` is a field whose value is the record's own text -- and quietly widening that
    to ``./@id`` would change what a config means without changing what it says.
    """
    if candidate.strip() != ".":
        return
    spec = parse_field_path(candidate, NAMESPACES)
    assert spec.segments == ()
    assert spec.attribute is None


@PROPERTY_SETTINGS
@given(st.one_of(st.text(), MALFORMED, FORMS))
def test_an_accepted_field_path_has_legal_segments_and_at_most_one_attribute(
    candidate: str,
) -> None:
    """★ The shape property, because a wrong segment is a wrong *value*, not a failure.

    Every accepted element segment must be a legal XML name or a braced ``{uri}local``, and
    the attribute -- when there is one -- must be too. This is what catches a resolver
    handing back the bare local name for a prefixed segment, which matches nothing and
    looks like an empty field.
    """
    spec = accepts_or_refuses(
        lambda: parse_field_path(candidate, NAMESPACES),
        expected=FIELD_PATH_ERRORS,
        label=f"parse_field_path({candidate!r})",
    )
    if spec is None:
        return
    for tag in spec.segments:
        assert _is_qualified_name(tag), f"{tag!r} is neither a legal name nor braced: {candidate!r}"
    if spec.attribute is not None:
        assert _is_qualified_name(spec.attribute), (
            f"attribute {spec.attribute!r} is not a legal qualified name: {candidate!r}"
        )


def _is_qualified_name(tag: str) -> bool:
    if tag.startswith("{"):
        uri, _, local = tag[1:].partition("}")
        return bool(uri) and bool(BARE_SEGMENT.match(local))
    return bool(BARE_SEGMENT.match(tag))


@PROPERTY_SETTINGS
@given(st.lists(st.sampled_from(VALID_NAMES), min_size=1, max_size=4))
def test_an_attribute_before_the_last_segment_is_always_refused(names: list[str]) -> None:
    """★ ``@`` may only appear on the final segment, and this asks the machine to check it.

    Built rather than sampled, so the property covers every position in a path of up to
    four segments rather than the two positions a hand-written test would list.
    """
    for position in range(len(names)):
        mangled = list(names)
        mangled[position] = "@" + mangled[position]
        candidate = "/".join(mangled)
        if candidate.split("/")[-1].startswith("@"):
            continue  # that position *is* the last segment, which is the legal case
        try:
            parse_field_path(candidate, NAMESPACES)
        except FieldPathError:
            continue
        raise AssertionError(
            f"an attribute at position {position} of {len(names)} was accepted: {candidate!r}"
        )


@PROPERTY_SETTINGS
@given(st.lists(st.sampled_from(VALID_NAMES), min_size=1, max_size=3))
def test_a_path_ending_in_an_attribute_reads_that_attribute(names: list[str]) -> None:
    """The converse: when ``@name`` is last, it must be an attribute and not an element.

    Together with the property above this pins the grammar from both sides, so a change
    that moved the ``@`` handling cannot pass one and fail only in the other direction.
    """
    attribute = "attr"
    candidate = "/".join([*names, "@" + attribute])
    spec = parse_field_path(candidate, NAMESPACES)
    assert spec.attribute is not None, f"{candidate!r} lost its attribute"
    assert spec.attribute == attribute or spec.attribute.endswith("}" + attribute)
    assert len(spec.segments) == len(names), (
        f"{candidate!r} should have {len(names)} element segments, got {spec.segments!r}"
    )


@PROPERTY_SETTINGS
@given(st.sampled_from(VALID_NAMES))
def test_a_default_namespace_applies_to_elements_but_never_to_attributes(name: str) -> None:
    """★ The asymmetry the field path docstring promises, stated as a property.

    ``namespaces={"": "urn:x"}`` must qualify the element ``name`` and must leave the
    attribute ``@name`` unqualified. Applying the default namespace to an attribute is a
    classic XML mistake -- ``xmlns`` does not apply to attributes -- and it would turn
    every attribute in a namespaced document into a silently missing one.
    """
    qualified = parse_field_path(name, {"": "urn:x"})
    assert qualified.segments == ("{urn:x}" + name,)

    with_attribute = parse_field_path(f"{name}/@id", {"": "urn:x"})
    assert with_attribute.attribute == "id", (
        f"the default namespace leaked onto an attribute: {with_attribute.attribute!r}"
    )
