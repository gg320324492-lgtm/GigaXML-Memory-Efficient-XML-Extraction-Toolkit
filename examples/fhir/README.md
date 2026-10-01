# FHIR R4 — a real schema, and a document whose values live in attributes

The [Wikipedia](../wikipedia/) example shows the memory claim and the
[PubMed](../pubmed/) one shows awkward shapes. This one shows the shape neither of them
has, and it is the reason it is here at all: **a schema**.

MediaWiki's namespace, PubMed's nesting and ERP's generated rows all live in the
*document*. None of them puts a schema anywhere, so until this milestone the project had
never been pointed at a real XSD — only at ones written for its own tests. Pointing it at
one found two defects within a minute. Both are pinned in
`tests/golden/test_known_defects.py` and neither is fixed; see
[`REAL-WORLD-VALIDATION.md`](../../REAL-WORLD-VALIDATION.md) for the measurements and for
why that is what the milestone asked for.

## Run it

```bash
# 1. fetch. ~3 MB of schema plus five small instances; the crawl is discovered, not listed
python examples/fhir/fetch.py

# 2. extract, from the repository root
python -m gigaxml.cli extract data/fhir/examples/patient-example.xml \
    -c examples/fhir/patients.yaml \
    -o out/patients.csv --format csv --report out/patients-report.json
```

What you should see:

```
id,gender,birth_date,active,family,given
example,male,1974-12-25,true,Chalmers,Peter
```

**One row is the whole of it, and that is not a mistake.** HL7 publishes these five files
as worked examples of each resource type, not as a corpus — there is no larger public FHIR
XML dump of this shape, which is why the example is about shape rather than volume, the
same way [PubMed](../pubmed/) is.

## The shape: the value is in the attribute

Every primitive in FHIR's XML is an element carrying a `value` attribute, not element
text:

```xml
<family value="Chalmers"/>
<birthDate value="1974-12-25"/>
<active value="true"/>
<system value="urn:oid:1.2.36.146.595.217.0.1"/>
```

**157 of the 304 elements in these five files carry a `value` attribute.** It is the
majority shape here, not an edge case — and a field path that stops at `<family>` gets
nothing at all, because there is no text there to read. Every field in
[`patients.yaml`](patients.yaml) therefore ends in `@value`.

The other three shapes, briefly, because the others already cover their own:

* **a default namespace**, exactly like MediaWiki — `xmlns="http://hl7.org/fhir"`, so a
  path written without the prefix does not resolve;
* **a foreign namespace inside the record** — `<div xmlns="http://www.w3.org/1999/xhtml">`
  carries the narrative text, with `<b>`, `<a>` and `<p>` in *that* namespace, inside a
  FHIR element;
* **same-name siblings with different shapes** — this document has three `<name>`
  elements, and the second has no `<family>` while the third has a `<period>`.

## Two things this example deliberately does not do

**`family` and `given` are the first `<name>`, not the patient.** gigaxml is a
one-record-one-row extractor, so a record's repeating children do not become columns and
the config's choice of which one to read is a decision the config makes. Writing this
down is the same reason [PubMed](../pubmed/) documents its authors limit: the alternative
is a tool deciding what your data means.

**The schema is fetched but not used by this config.** A config can name a `schema:` and
gigaxml will use it to type the fields — and against these real files that currently
fails, which is the defect above. Pointing `patients.yaml` at it would produce a red run
on a red build for a reason that has nothing to do with the extraction. The `date` and
`bool` types here are what the schema would have supplied, written out by hand instead.

## The schema set, if you want to look at it

```
data/fhir/xsd/
  fhir-all.xsd          9,645 B   146 xs:include, and nothing else
  fhir-base.xsd                reachable only through those includes
  xml.xsd                      and so is this -- W3C's, shipped as a local copy
  ...146 more
```

Two of those three matter more than their size suggests:

* **`fhir-all.xsd` names 146 of the 150 files.** The rest arrive through what those
  include, so a fetch script that copied its own include list would be four files short
  and would fail with a message about a missing base type rather than a missing download.
  [`fetch.py`](fetch.py) follows the graph instead.
* **`xml.xsd` is the W3C XML-namespace schema, canonically published at
  `http://www.w3.org/2001/xml.xsd`.** HL7 ships a local copy rather than pointing at that
  URL — which is what lets the whole set compile inside gigaxml's schema sandbox. It is
  also the one namespace `xmlschema` bundles internally, and that is the detail behind the
  second defect: when this import is blocked, the library quietly satisfies it from its
  own copy and the compile succeeds.

## What it does not tell you

The **memory claim** is not demonstrated here. A 3.9 KB document leaves the peak at
whatever the interpreter costs before it starts, and a small document cannot demonstrate a
bound. That claim is the [Wikipedia](../wikipedia/) example's, at 1.6 GB.
