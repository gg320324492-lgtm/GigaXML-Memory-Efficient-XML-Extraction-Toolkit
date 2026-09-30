# PubMed — one day of the baseline export, 4,989 citations

[Wikipedia](../wikipedia/) shows the memory claim. This one shows the **shapes**, and it
is the example to read when you are deciding whether this tool fits your XML: the shapes
here are the awkward ones, and two of them are limits rather than features.

The data is a single day of [PubMed's baseline
export](https://ftp.ncbi.nlm.nih.gov/pubmed/baseline/) — every citation PubMed added on
one day, around 13 MB compressed.

## Run it

```bash
# 1. fetch a day. The file name is discovered from the directory listing, never pinned --
#    NCBI deletes files as they age out of a rolling window.
python examples/pubmed/fetch.py

# 2. extract, straight out of the .gz (no decompression step, no temp file)
python -m gigaxml.cli extract data/pubmed-baseline.xml.gz \
    -c examples/pubmed/config.yaml \
    -o out/pubmed.csv --format csv --report out/pubmed-report.json
```

Run the extract from the repository root, which is where the config path points. The
`.xml.gz` goes in by extension — no flag, and no decompressed copy on disk.

## What you should see

`fetch.py` records which file it got and its sha256 in
[`pubmed.json`](pubmed.json). Check the extraction against that:

```bash
python -c "import json; d=json.load(open('examples/pubmed/pubmed.json')); \
  print(d['name'], f\"{d['bytes_gz']:,}\", d['sha256_gz'][:16])"

python -c "import json; print(json.load(open('out/pubmed-report.json'))['rows'])"
# -> 4989
```

**4,989 is the count for the file you fetched, not a number to expect.** These are daily
files: tomorrow's fetch gives a different citation count, and a different sha256. What
does not change is the relationship — the rows are the number of `<PubmedArticle>`
elements, and `gigaxml inspect` counts the same elements the same way:

```bash
python -m gigaxml.cli inspect data/pubmed-baseline.xml.gz --max-paths 30 --json | \
  python -c "import json,sys; print(next(c['count'] for c in json.load(sys.stdin)['candidates'] \
             if c['path']=='/PubmedArticleSet/PubmedArticle'))"
# -> 4989
```

**`--max-paths 30` is not decoration, and dropping it gives a different answer.** The
candidate list is ranked, and on this document the two highest-scoring candidates are
*inside* articles — `PubmedData/ReferenceList/Reference/Citation` at 82,113 occurrences
and `AuthorList/Author/AffiliationInfo` at 42,448 — against 4,989 articles. Raise the path
table's cap and they are all in it; leave it at the default and the article itself is not
listed among the top ten. `inspect` says so when the table fills ("a further 566,829
element occurrences are at paths not listed"), and the count you are after can be one of
those.

**On the peak memory, be careful what it means here.** This run reported a peak of about
35 MiB against a 13 MB input, and that is not a contradiction — at this size the
interpreter's own footprint is the whole of it, and the document contributes nothing
visible. This example is about shape, not about memory; the memory claim is the
[Wikipedia example](../wikipedia/), where 1.6 GB of input leaves the peak at about 36 MiB.
A small document cannot demonstrate a bound.

## The shapes, and the two that are limits

**Attributes beside text, at two levels.** `MedlineCitation/@IndexingMethod` and
`ISSN/@IssnType` sit on elements that also carry a value, so a row holds both halves of a
fact that belongs together. `pmid` is the value and `pmid_version` is its `Version="1"`.

**Deep paths.** The journal abbreviation is four levels down:
`MedlineCitation/Article/Journal/ISOAbbreviation`.

### Limit: a date the tool will not reassemble

`DateRevised` is not a date. It is three elements — `2026`, `01`, `28` — and elsewhere in
the same article `PubDate/Month` says `Jan`. There is no type that spans that, and
**gigaxml does not concatenate the pieces for you**: the config extracts `revised_year`,
`revised_month` and `revised_day` as three columns, and turning them into a date is the
reader's call.

That is worth stating plainly because the alternative — a config that quietly builds
`2026-01-28` — is a tool deciding what your data means. Where the padding is a choice
rather than a fact (`01` versus `Jan`) the tool has no way to know which convention you
meant, and getting it wrong produces a plausible wrong date rather than an error.

### Limit: repeated children do not become columns

Every article here has several `AbstractText` and several `Author` — 34,978 authors
across 4,989 articles in one day. **Neither becomes a column**, and the tool does not
invent one: this is a one-record-one-row extractor, so a record's repeating children are
not something it can flatten without deciding which of them to keep.

The honest way to get at them is to make them the record. `gigaxml inspect` names the
candidates for exactly this decision — on the file fetched here, with `--max-paths 30`:

```
  1. /PubmedArticleSet/PubmedArticle/MedlineCitation/Article/AuthorList/Author/AffiliationInfo   [nested inside #2]
  2. /PubmedArticleSet/PubmedArticle/MedlineCitation/Article/AuthorList/Author                   [nested inside #4]
  3. /PubmedArticleSet/PubmedArticle/MedlineCitation/Article/ELocationID                        [nested inside #4]
  4. /PubmedArticleSet/PubmedArticle
  5. /PubmedArticleSet/PubmedArticle/MedlineCitation/DateRevised                                  [nested inside #4]
  6. /PubmedArticleSet/PubmedArticle/MedlineCitation/PMID                                        [nested inside #4]
```

Candidate 2 gives you one row per author — 34,978 of them — and each row can still reach
back up to the article, because a field path is relative to the record and can walk
outward. Candidate 4 gives one row per article and no authors at all. **Which one you
want is a question about your data, and the choice is yours to make rather than the
tool's to guess.** The nested candidates are marked `[nested inside #N]` for the same
reason: they are repeating structures *within* the article, not competing records — note
that candidate 1 is itself nested inside candidate 2, which is inside candidate 4.
