# gigaxml config format

The YAML document passed to `gigaxml extract -c` / `gigaxml sample -c`. This file
describes what to read and how to type it; it never affects *where* the records come
from or how much memory the run uses.

## Versioning

```yaml
version: 1
```

| | |
|---|---|
| Type | integer, exactly `1` |
| Required | **No.** A config without `version` is this project's own format from before versioning and loads exactly as it always has. |
| Today's value | `1` |

`version` is the only version marker in the config. It is checked by type, not by
comparison: `true`, `1.0` and `"1"` are all refused, because each of those equals `1`
under Python's `==` while not being the integer this key means.

## Required keys

| Key | Type | Notes |
|---|---|---|
| `record` | string | The element path to read, e.g. `/catalog/products/product`. Parsed with root-anchoring rules; `//` means any ancestors. |
| `fields` | mapping | Field name → definition. **Must be non-empty** — an empty mapping would extract nothing, and that is refused rather than run. |

Each entry in `fields` requires a `path`; `type` and `required` are optional within it.

## Optional keys

| Key | Type | Default | Notes |
|---|---|---|---|
| `namespaces` | mapping | none | Prefix → URI. The empty-string key is the default namespace. Applies to the record path and every field path. |
| `on_error` | `"abort"` \| `"quarantine"` | `abort` | What one unconvertible record does. `abort` stops the run; `quarantine` logs it and continues. |
| `schema` | string | none | Path to an XSD. Read through the optional `xsd` extra; changes field *types* only, never which records match. |
| `version` | integer | — | See above. |

## Unknown keys

**Refused.** An unrecognised top-level key is a `ConfigError` naming the key and the
keys that are allowed.

This is deliberate. Silently ignoring a typo (`type: flot`, or a misspelled `on_error`)
would fall back to a default and produce an output file with wrong contents — which
looks exactly like a correct run. A config that refuses to start is strictly better.

`version` is therefore not "a key that is tolerated": it is a checked key. Admitting it
without checking its value would let `version: 2` load as version 1.

## Unsupported versions

A `version` other than the integer `1` is refused with a message that says which
version this build reads:

```
<path> declares config version 2, but this build reads version 1. Set 'version: 1',
or remove the key if the file predates versioning.
```

The message is worded differently from an unknown-key refusal on purpose: an unknown
key asks you to *remove* it, an unread version asks you to *change* it. A single
message cannot tell you which to do.

## Compatibility and upgrade

**There is no migration mechanism today.** This build reads version 1 and nothing else;
a config declaring a different version is refused rather than best-effort interpreted.

Upgrading a config is manual — and, at version 1, consists of nothing: every config
this project has ever shipped or documented is still valid.

**No upgrade tool and no forward-compatibility promise exist.** They would be recorded
here if they did.
