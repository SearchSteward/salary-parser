# salary-parser

[![tests](https://github.com/searchsteward/salary-parser/actions/workflows/tests.yml/badge.svg)](https://github.com/searchsteward/salary-parser/actions/workflows/tests.yml) [![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Extract real salaries from job-posting text — without fabricating them.**

The salary extractor from [SearchSteward](https://searchsteward.com)'s ingest
pipeline, published as a standalone, dependency-free Python library. It is the
code that decides, for every posting we scrape, whether a number is a salary
at all.

Salary extraction looks trivial and isn't. The naive regex approach — "find a
dollar amount, call it a salary" — confidently reports that a job pays:

- **$20,800** because the posting says *"10-15 hours per week"*
- **$600,000** because *"two Saturdays per month"* rescaled a $50k commission by 12
- **$166,400** because *"Travel 80%"* was read as an $80/hour rate
- **$401,000** because the benefits section mentions a *401(k)*
- **$60,000** for a role that actually pays **€60,000**

Every rule in this library exists because one of those happened on real
postings. The core invariant:

> **A number becomes a salary only if the text says it is one, and it is
> converted using the pay cadence attached to *that* number.**

## Install

```bash
# One file, stdlib only — vendor it:
curl -O https://raw.githubusercontent.com/searchsteward/salary-parser/main/salary_parser.py
```

A PyPI package (`steward-salary-parser`) is planned; the file above is the same code.

## Usage

```python
from salary_parser import derive_salary, extract_salary_from_text, extract_salary_from_explicit_text

# The main entry point: an explicit salary field (if your source has one),
# falling back to description prose.
derive_salary("$45 - $55 per hour", None)
# {'min': 93600.0, 'max': 114400.0, 'currency': 'USD', 'source': 'text_extracted'}

# Description prose is context-gated: numbers need a salary keyword in their sentence.
extract_salary_from_text("The base salary range for this role is $105,000 — $206,000 USD")
# {'min': 105000.0, 'max': 206000.0, ...}

extract_salary_from_text("You will manage a budget of $250,000")
# None — a budget is not a salary

# An explicit salary field is context-exempt but still shape-gated.
extract_salary_from_explicit_text("80k-120k")
# {'min': 80000.0, 'max': 120000.0, ...}
```

## The rules

1. **Context, sentence-scoped.** In prose, a salary keyword ("salary",
   "compensation", "pay rate", "/hour"…) licenses numbers in *its own
   sentence* only. *"Up to $20k in fertility services. The base salary range
   is $163,400…"* must not adopt the benefit as the floor. Abbreviation
   periods ("U.S.") do not terminate a sentence.
2. **Shape gating.** A number needs a `$` prefix, a `k` suffix, or to be an
   *operand* of an explicit range (`140,000 to 180,000`). Sitting near an
   unrelated range doesn't count.
3. **Negative units.** A number immediately followed by
   hours/weeks/years/employees/customers/% is a quantity, not money — and it
   poisons its range partners (*"10-15 hours"* kills the 10 too).
4. **Per-number cadence.** "/hour" or "per month" annualizes the number it
   trails (×2080 / ×12), clipped at the clause boundary. One incidental
   "/hour" in a document must not rescale every number in it.
5. **401(k)/403(b) sanitization** before anything else. No $401k phantoms.
6. **Bounds** of $20k–$900k annual, applied *after* cadence conversion, so
   $45/hour is range-checked as $93,600.
7. **Foreign currency yields nothing.** `EUR 60,000`, `£`, `C$`, `A$` — a
   non-USD amount returns `None` rather than a silently wrong USD number.
8. **HTML-entity soup is cleaned first.** Descriptions often arrive as
   `&lt;span&gt;$72,000&lt;/span&gt;&lt;span&gt;&amp;mdash;&lt;/span&gt;…`
   (sometimes double-escaped). Entities are unescaped to a fixed point and
   tags become spaces, so `$72,000</span><span>$91,000` doesn't glue into
   `$72,000$91,000`.

## Scope

- **US-annual output.** Hourly/monthly figures are annualized; foreign
  currencies are rejected, not converted.
- **Deterministic, pure, stdlib-only.** No I/O, no model calls, no
  dependencies. The test suite (86 cases, most of them real-posting
  regressions) is the specification.

## About

Built and maintained by [SearchSteward](https://searchsteward.com) — a
job-search radar that watches 40,000+ company career pages and scores every
new opening against your résumé. This library is one of the pieces we
open-source because transparency is the feature: if a tool filters jobs by
salary, you deserve to see how it reads one.

## License

[MIT](LICENSE). Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).
