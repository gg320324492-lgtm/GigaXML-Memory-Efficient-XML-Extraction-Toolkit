# Licence and provenance — `xsd/`

**The 150 files in `xsd/` are HL7's, not this project's.** They are stored here unchanged:
every byte is what HL7 published, and `examples/fhir/fhir.json` records a sha256 for each
one plus a digest over the whole set. Nothing in this repository has edited them, and
nothing should — to refresh the set, re-fetch it with [`fetch.py`](fetch.py).

| | |
|---|---|
| **What** | the HL7 FHIR R4 (4.0.1) XML schema distribution |
| **Where from** | <https://www.hl7.org/fhir/R4/> — one seed, `fhir-all.xsd`, then the transitive closure of its `xs:include` / `xs:import` graph |
| **Fetched** | 2026-10-01 |
| **Files** | 150, totalling 3,077,151 bytes |
| **Digests** | [`fhir.json`](fhir.json) — a per-file sha256 each, and `set_sha256` over all of them |
| **Why it is here** | `tests/integration/test_xsd_namespaces.py` compiles the whole set. It could not do that while the files lived in `data/`, which is gitignored and so fetched per machine. |

This is **not** covered by this repository's own `LICENSE`. The two are separate, and the
one that applies to a file in `xsd/` is the one HL7 or W3C wrote on it.

## What the files themselves say

**148 of the 150 carry the same BSD 3-Clause header**, in the XML comment each file opens
with. Verbatim, from `fhir-all.xsd` (the other 147 differ only in the trailing
"Generated on … for FHIR v4.0.1" line):

```
Copyright (c) 2011+, HL7, Inc
 All rights reserved.

 Redistribution and use in source and binary forms, with or without modification,
 are permitted provided that the following conditions are met:

 * Redistributions of source code must retain the above copyright notice, this
 list of conditions and the following disclaimer.
 * Redistributions in binary form must reproduce the above copyright notice,
 this list of conditions and the following disclaimer in the documentation
 and/or other materials provided with the distribution.
 * Neither the name of HL7 nor the names of its contributors may be used to
 endorse or promote products derived from this software without specific
 prior written permission.

 THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
 ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
 WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
 IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT,
 INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT
 NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
 PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
 WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
 ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
 POSSIBILITY OF SUCH DAMAGE.
```

Redistribution is permitted, and two of the three conditions are about the notice
travelling with the copy — which is what this file and the headers themselves do.

## What HL7 says about the specification

HL7's licence page for this release, <https://www.hl7.org/fhir/R4/license.html> §2.20.2
(FHIR v4.0.1), read on **2026-10-02**:

> Copyright © 2011+ HL7. This specification (specifically the set of materials included in
> the fhir-spec.zip file available from the Downloads page of this specification) is produced
> by HL7 under the terms of HL7® Governance and Operations Manual relating to Intellectual
> Property (Section 16), specifically its copyright, trademark and patent provisions. This
> document is licensed under Creative Commons "No Rights Reserved" (CC0).

**The two statements are about different things, and the difference is worth stating rather
than smoothing over.** CC0 is HL7's licence for the *specification*; the BSD 3-Clause header
is what the *schema files* carry. CC0 is the more permissive of the two and would satisfy
anything BSD-3-Clause asks for, so nothing here is at risk either way — but the more
specific statement is the one that travels with the bytes, and it is the one reproduced
above. This repository does not claim CC0 over these files, and does not need it.

## The two files that are not HL7's

`fhir-all.xsd` reaches 146 of the 150 directly; these two arrive only through what it
includes, and neither carries the HL7 header.

**`xml.xsd` — the W3C XML-namespace schema, shipped here as a local copy.** HL7
redistributes it rather than pointing at <http://www.w3.org/2001/xml.xsd>, which is what
lets the whole set compile inside gigaxml's schema sandbox (see the example
[README](README.md)). Checked against the W3C original on 2026-10-02: same content,
reformatted — 9,568 bytes here against 8,836 there, and the differences are the XML
declaration, indentation, line breaks and one added comment. It carries no licence header
of its own; W3C documents are published under the **W3C Software and Document License**
(<https://www.w3.org/copyright/software-license-2023/>, read on 2026-10-02), which grants
copying and redistribution provided the notice travels with the copy.

**`fhir-xhtml.xsd` — HL7's "pared down modification of the XHTML schema published by the
W3C"**, in its own words in the comment it opens with. It carries no HL7 header either; what
it carries is the notice embedded in the XHTML DTD it is derived from:

> Copyright (c) 1998-2002 W3C (MIT, INRIA, Keio), All Rights Reserved.

Same W3C licence as above.

## Trademarks

HL7 asks, on the page quoted above, that any use of the marks say so:

> "HL7, FHIR and the FHIR flame design are the registered trademarks of Health Level Seven
> International and their use does not constitute endorsement by HL7."

It also asks that "HL7", "FHIR" and the logo carry the ® symbol, and that the standard be
named "the HL7® FHIR® standard" the first time in any document. This repository names the
source in prose and ships no logo.

## What was not verified

The per-file sha256 values in `fhir.json`, and the 150-file `set_sha256`, are this
repository's own measurement of what it fetched — they are not HL7's and they do not speak
to the licence. Everything above about the licence was read from HL7 and W3C on the dates
given; nothing here is written from memory.