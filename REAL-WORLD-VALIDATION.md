# Real-world validation

Every number here was measured by running the command beside it, on the machine named,
on the date named. Where a number does not repeat, the document says so rather than
rounding it into a shape that looks stable.

**This file is at the repository root on purpose.** `docs/` is gitignored in this project,
so a document written there is a document nobody cloning the repository can see. See
[`SECURITY.md`](SECURITY.md), which makes the same point about the policy note it links
to.

| | Dataset | Bytes | Expected | Actual | Match |
|---|---|---|---|---|---|
| 1 | [Simple English Wikipedia](examples/wikipedia/) | 1,696,517,417 | 560,605 | 560,605 | ✓ |
| 2 | [PubMed baseline](examples/pubmed/) | 13,837,691 gz | 4,989 | 4,989 | ✓ |
| 3 | [ERP](examples/erp/) *(generated)* | 52,708,114 | 145,600 / 48,533 | 145,600 / 48,533 | ✓ |
| 4 | [HL7 FHIR R4](examples/fhir/) | 3,077,151 schema + 14,124 data | 1 | 1 | ✓ |

**"Expected" and "actual" are counted by two different programs.** Expected comes from
`gigaxml inspect`, which counts element occurrences while streaming; actual comes from
`gigaxml extract`, which writes rows. For the generated ERP file the expected count comes
from the generator's own manifest instead, which is a third measurement again. Agreement
between them is a check, not a tautology — a config with a wrong record path produces an
expected count and an actual count that disagree, which is exactly what the table above is
for.

Tool version for every run below: **gigaxml 1.2.1**, CPython 3.13.14 (Windows).

---

## 1. Simple English Wikipedia

Every article on Simple Wikipedia, in MediaWiki's XML export format. This is the memory
claim's document.

| | |
|---|---|
| **Source URL** | <https://dumps.wikimedia.org/simplewiki/latest/simplewiki-latest-pages-articles.xml.bz2> — reachable, HTTP 200 |
| **Fetched** | 2026-10-01T19:03:30Z |
| **Config** | [`examples/wikipedia/config.yaml`](examples/wikipedia/config.yaml) |
| **Expected records** | **560,605** (`inspect`, path `/mediawiki/page`) |
| **Actual records** | **560,605** rows; rejected 0; status `ok` |
| **Peak RSS** | 35.44 MiB against 1,617.92 MiB of input — **2.2%** |
| **Elapsed** | 21.671 s (25,869 records/s) |
**Archive hash** (`sha256_bz2`, 356,186,307 bytes):

```
6832fd106ae0e4734a349d6351709fc219ccebad5850234214a57e653f7812f8
```

**XML hash** (`sha256_xml`, 1,696,517,417 bytes) — the archive hash is not enough, since
what was extracted is the decompressed file and a decompression step is exactly where a
different file could appear:

```
2575aba62792ac2c1a907ac5196c0e3681b4d6cd5d1ba58c1c1a06df13629c64
```

★ **Both hashes are byte-identical to the values recorded on 2026-09-27.** The dump was
not regenerated in the five days between. That is a fact about this file in this window,
not a property of dumps, which are refreshed on Wikimedia's schedule — the recorded
`fetched_at` moved forward while the bytes did not, and the two facts are recorded
separately for that reason.

`inspect` lists 7 candidate paths at `--max-paths 30`, and `/mediawiki/page` is **5th**,
behind `/mediawiki/page/redirect` at 124,218 occurrences. Matching on the path is not
optional; matching on rank finds the redirect.

## 2. PubMed baseline

One day of PubMed's baseline export — every citation added that day, straight out of the
`.gz` with no decompression step and no temp file.

| | |
|---|---|
| **Source URL** | <https://ftp.ncbi.nlm.nih.gov/pubmed/baseline/pubmed26n1334.xml.gz> — reachable, HTTP 200 |
| **Downloaded** | **2026-09-30T08:11:07Z** — verified, *not re-fetched*, this run (see below) |
| **Config** | [`examples/pubmed/config.yaml`](examples/pubmed/config.yaml) |
| **Expected records** | **4,989** (`inspect --max-paths 30`, path `/PubmedArticleSet/PubmedArticle`, ranked **4th**) |
| **Actual records** | **4,989** rows; rejected 0; status `ok` |
| **Peak RSS** | 35.99 MiB against a 13 MB input — see the note on what that means |

**Archive hash** (`sha256_gz`, 13,837,691 bytes):

```
d241061caa1b5631aa5a0bb429da04b8a6a3a7b6240ffa275008a07924f79aee
```

★ **This file was not downloaded again on 2026-10-02.** The file on disk was hashed and
matched the recorded digest, which is the contract
[`fetch.py`](examples/pubmed/fetch.py) establishes — a corruption check, not a re-fetch.
Re-fetching would have taken whatever day NCBI lists now, and would have rewritten the
README's documented count for no gain. So the download date is 2026-09-30 and the
verification date is 2026-10-02, and both are stated rather than merged.

There is no XML-body hash for this dataset **because there is no decompressed file**:
gigaxml reads the `.gz` directly, so the archive digest *is* the digest of the bytes that
were parsed.

**The peak means nothing here and should not be quoted as if it did.** At 13 MB the
interpreter's own footprint is the whole of it; a small document cannot demonstrate a
bound. The memory claim is section 1's, at 1.6 GB.

## 3. ERP — generated

A product master and an order log in one document, generated with `--seed 42` so every
number is exact and repeatable.

| | |
|---|---|
| **Source** | not downloaded — `gigaxml generate --size 50MB --seed 42` |
| **Manifest** | [`out/erp.xml.manifest.json`](examples/erp/) — seed, record path, count, hash |
| **Configs** | [`products.yaml`](examples/erp/products.yaml), [`orders.yaml`](examples/erp/orders.yaml) |
| **Expected (products)** | **145,600** — the generator's own `record_count` |
| **Actual (products)** | **145,600** rows; rejected 0; status `ok`; peak 35.57 MiB; 4.006 s |
| **Expected (orders)** | **48,533** — the count the README documents for this seed |
| **Actual (orders)** | **48,533** rows; rejected 0; status `ok`; peak 33.21 MiB; 1.694 s |

**Document hash** (52,708,114 bytes) — recomputed from the file after generation and
compared with the manifest:

```
f8feb02d8a2345b448eb5a1eaa859b5d133e33a65ea435ff0e88947143c53f8d
```

★ The orders count is the weaker half of this pair: 48,533 comes from documentation, not
from the generator's manifest, so it is a claim being checked rather than two independent
measurements agreeing. It is left as it is rather than dressed up.

## 4. HL7 FHIR R4

A real health-interchange schema set and the five instances HL7 publishes beside it.
Added in M16, which is also when this project first pointed a schema at anything not
written for its own tests; it is the only one of the four that ships a schema.

### 4a. The schema set

| | |
|---|---|
| **Source URL** | <https://www.hl7.org/fhir/R4/fhir-all.xsd> — reachable, HTTP 200 |
| **Fetched** | 2026-10-01T19:19:47Z |
| **Fetch method** | discovered — the transitive closure of `xs:include` / `xs:import` from one seed |
| **Files** | **150**, totalling **3,077,151** bytes |
| **Per-file hashes** | all 150 recorded in [`fhir.json`](examples/fhir/fhir.json) |

There is no single archive and so no single archive hash. What is recorded instead is a
`set_sha256` over every file's name and bytes in name order, so "did anything change" is
one command, plus a per-file sha256 for each of the 150 so "what changed" is also one:

```
set_sha256 = daa03c4437e7ff39372d17322e159210dedf12d1b337a0f1bbde7f6f61c8df6b
```

**What the set's references actually are**, counted by `fetch.py` on every run and
recorded in the same file:

| | |
|---|---|
| `xs:include` / `xs:import` edges | **296** (293 include, 3 import) |
| Remote (`http`/`https`) `schemaLocation` | **0** |
| Absolute or `../` `schemaLocation` | **0** |
| Files carrying a DOCTYPE | **0** |

★ **`fhir-all.xsd` names 146 of the 150 files.** `fhir-base.xsd`, `fhir-xhtml.xsd` and
`xml.xsd` arrive only through what those include — so a fetch script that copied the
entry file's own include list is four files short, and the failure it produces is a
message about a missing base type rather than a missing download. That is what happened
here first, and it is why the crawl follows the graph.

### 4b. The instances

Five files, 14,124 bytes, fetched from the same directory:

| File | Bytes | sha256 (first 16) |
|---|---|---|
| `patient-example.xml` | 3,874 | `606695d227a8669d` |
| `bundle-example.xml` | 3,623 | `b63afa971534afd8` |
| `observation-example.xml` | 3,662 | `03906eee9a5eb59f` |
| `condition-example.xml` | 1,706 | `659f035652a0d139` |
| `practitioner-example.xml` | 1,259 | `22534725be096261` |

| | |
|---|---|
| **Config** | [`examples/fhir/patients.yaml`](examples/fhir/patients.yaml) |
| **Input** | `data/fhir/examples/patient-example.xml` |
| **Expected records** | **1** (`inspect`, path `/Patient`) |
| **Actual records** | **1** row; rejected 0; status `ok`; peak 31.60 MiB; 0.002 s |
| **Row** | `example,male,1974-12-25,true,Chalmers,Peter` |

**Expected is 1 because this is one `<Patient>` document, and saying so is the point.**
HL7 publishes these as worked examples of each resource type; there is no larger public
FHIR XML dump of this shape. More rows would not make the example say anything it does not
already say, which is why this dataset earns its place on shape rather than volume.

★ Across the five files: **304 elements, of which 157 carry a `value` attribute.** In this
format the scalar lives in the attribute, not in element text, and every field path in the
config ends in `@value`. No other dataset here has that shape — see the
[example README](examples/fhir/README.md).

---

## Criterion D — the sandbox against a real XSD

This was the first time this project pointed a schema at anything not written for its own
tests. The short form:

| | |
|---|---|
| Real 150-file set compiles under `allow="sandbox"`, `defuse="always"` | **yes**, 146 global elements, ~2.5 s |
| A name the schema genuinely does not declare | still refused |
| **A name the schema *does* declare** | **also refused** — defect, see below; repaired since |
| Remote `schemaLocation` | file not read; **0 outbound connections** |
| `../` escape | file not read — but the refusal is downgraded to a warning; reported since |

**The reach boundary holds; the report of the refusal does not.** Both statements were
measured by watching `socket.connect` during compilation, not inferred from a message. The
two rows marked above were true when measured and were both fixed afterwards — a declared
name is read again, and a downgraded refusal is now raised instead of passed over. The reach
boundary was never the thing at risk.

## Criterion E — two real defects, found, pinned, and since repaired

Both were pinned rather than fixed, in M16's own instruction to pin and stop — and both were
repaired afterwards. The pins stay, because a pin that says a defect is gone and can be read
back is the record that it was ever there; what changed is what each test now asserts.

1. **`record_field_types` refuses every schema with a target namespace.** It looks the
   element up by bare local name; `xmlschema` keys a namespaced schema by `{ns}local` and
   returns `None`. The error then says the schema *declares no such element* — **while
   listing that element in the sentence**. On the FHIR set all 146 globals are refused.

2. **A blocked or missing `xs:import` compiles successfully.** `xmlschema` catches the
   block, downgrades it to `XMLSchemaImportWarning`, and satisfies the namespace from its
   own bundled copy. Deleting `xml.xsd` from the real set changes nothing visible: same
   146 globals, same `Patient` with 13 children, two warnings. M1's fixtures never hit
   this because every one imported a namespace `xmlschema` does not ship.

Both are pinned, with controls, in
[`tests/golden/test_known_defects.py`](tests/golden/test_known_defects.py).
