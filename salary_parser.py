"""Salary extraction from job-posting text.

Extracted from the SearchSteward ingest pipeline — the same rules that decide,
for every scraped posting, whether a number is a salary at all.

Invariant: a number becomes a salary only if the text says it is one, and it is
converted using the cadence attached to *that number*.

Rules:
- Sanitize 401k/403b patterns before extraction (no $401k phantoms)
- Context: a salary keyword licenses numbers in ITS SENTENCE, not merely within 80
  chars ("…up to $20k in fertility services. The base salary range is $163,400…"
  must not adopt the benefit as the floor). Applies to description text only.
- Shape gating: a number must have a $ prefix, a k-suffix, or be an *operand* of an
  explicit range. Co-location with an unrelated range is not shape.
- Negative units: a number immediately followed by hours/weeks/years/customers/% is a
  quantity, not money — and it neuters its range partners. Cadence phrases ("per
  month", "/hour") are NOT negative units; they denominate the amount.
- Cadence is per-number, read forward from the number and clipped at the clause end.
  One incidental "/hour" in a JD must not rescale every number in it.
- Bounds: 20k-900k annual, applied AFTER the per-number cadence multiplier.
- An explicit salary field is context-exempt; description prose never is.
- A non-USD marker (ISO code, €/£/C$/A$ …) yields no salary rather than a wrong
  number in a USD field.

Pure functions, no I/O, standard library only.
"""

from __future__ import annotations

import html
import re
from typing import Optional, Dict, Any


# Compiled regex constants — compiled once at module load.
# A pay CADENCE is salary context. An early vocabulary only knew annual phrasings, so a
# perfectly explicit "Pay rate: $20.00 hourly" yielded nothing from the gated path.
#
# The slash forms carry no leading \b (there is no word boundary before "/"), matching the
# same fix made to _HOURLY_PATTERN.
_CONTEXT_PATTERN = re.compile(
    r"\b(salary|salaried|compensation|pay range|pay rate|base pay|base salary|wage"
    r"|annual|annually|per year|yearly|ote|on[- ]?target"
    r"|hourly|per hour|per month|monthly)\b"
    r"|/hr\b|/hour\b|/month\b",
    re.IGNORECASE
)

_SHAPE_PATTERN = re.compile(
    r"\$?\s*([0-9]{1,6}(?:,\d{3})*(?:\.\d{1,2})?)\s*([kK]?)"
)

# Cadence detection patterns.
# A leading \b before "/hour" is wrong: at the start of a per-match window the text is
# "/hour", and there is no word boundary between start-of-string and "/". The slash forms
# therefore never matched when the number sat immediately before them ("$23/hour"), while
# "$22-$23/hour" matched only because the "3" supplied the boundary. Anchor \b on the word.
_MONTHLY_PATTERN = re.compile(
    r"(?:\bper\s+month\b|/month\b|\bmonthly\b)",
    re.IGNORECASE
)

_HOURLY_PATTERN = re.compile(
    r"(?:\bper\s+hour\b|/hr\b|/hour\b|\bhourly\b)",
    re.IGNORECASE
)

# Currency symbols and their ISO codes
_CURRENCY_MAP = {
    "£": "GBP",
    "€": "EUR",
    "¥": "JPY",
    "₹": "INR",
}

# False-positive guard patterns
_FALSE_POSITIVE_PATTERNS = [
    r"\b(raised|series [a-z]|funding|seed|venture)\b",
    r"\b(equity|stock grant|options|rsu)\b",
    r"\b(signing bonus)\b",
]


def _clean_markup(text: str) -> str:
    """Un-escape HTML entities, then strip tags. Both, in that order.

    Job descriptions frequently arrive HTML-ENTITY-escaped — `&lt;div class=&quot;...&gt;`,
    not `<div class="...">`. A tag-stripper alone never fires on these: the entity soup
    stays inline and the sentence-scoped context window cannot reach a salary keyword
    across it. Greenhouse pay-transparency blocks are the canonical case:

        ... Position Pay Range   $72,000  &mdash;  $91,000 USD ...

    Tags are replaced with a SPACE, not "": `$72,000</span><span>$91,000` must not become
    `$72,000$91,000`. `&nbsp;` un-escapes to U+00A0, which is not `\\s` to `re`, so it is
    normalised too.
    """
    if not text:
        return text
    # Un-escape to a FIXED POINT. Some sources double-escape: `&amp;mdash;` becomes the
    # literal text `&mdash;` after one pass, and only an em dash after two. Bounded so a
    # pathological payload cannot spin.
    unescaped = text
    for _ in range(3):
        once = html.unescape(unescaped)
        if once == unescaped:
            break
        unescaped = once
    without_tags = re.sub(r"<[^>]+>", " ", unescaped)
    return without_tags.replace("\xa0", " ")


# A non-USD amount has nowhere truthful to go in a USD-denominated result. Symbols alone
# were never enough — some boards ship `"EUR 60,000 - 80,000"` as an ISO CODE, so €60k
# would be read as $60k. Canadian/Australian postings write `C$`/`A$`, which contain a
# literal `$` and read as USD without this guard.
_FOREIGN_CURRENCY = re.compile(
    r"\b(COP|KRW|HUF|CRC|SGD|MXN|BRL|INR|JPY|EUR|GBP|CAD|CHF|SEK|NOK|DKK|PLN|ZAR|AUD|NZD)\b"
    r"|[€£¥₩₹]|(?<![A-Za-z])(?:CA|C|A|NZ|R)\$",
    re.IGNORECASE,
)


def _detect_currency(text: str) -> str:
    """Detect currency from symbols OR ISO codes. 'USD' means "no foreign marker found"."""
    for symbol, code in _CURRENCY_MAP.items():
        if symbol in text:
            return code
    match = _FOREIGN_CURRENCY.search(text or "")
    if match:
        token = match.group(0).strip().upper()
        return token if token.isalpha() and len(token) == 3 else "FOREIGN"
    return "USD"


def _is_foreign_currency(text: str) -> bool:
    """True when the text carries any non-USD marker. Such a row must yield no salary."""
    return bool(_FOREIGN_CURRENCY.search(text or ""))


# Cadence is read from a window that starts at the number and runs forward: cadence
# always trails its amount ("$8,000 per month"), never precedes it. So "Monthly stipend
# of $2,000" does not annualize the 2,000 — and shouldn't, it's a stipend, not a salary.
_CADENCE_LOOKAHEAD = 20

# The window must not leak past the end of the clause, or the next clause's cadence is
# stolen: in "…$150,000 per year. Monthly stipend…" a flat 20-char lookahead from
# `150,000` reaches "Monthly", annualizes to 1.8M, and the bounds check silently
# discards a correct max.
#
# Stop at a clause terminator only — NOT at the next number. A range's cadence trails its
# right operand ("$45.00 - $55.00 per hour"), so the left operand has to read straight
# through `55.00` to find "per hour". Stopping at digits would strip every range's floor.
# A period only terminates a clause when it isn't a decimal point ("$55.00 per hour").
_CLAUSE_END_PATTERN = re.compile(r"\.(?!\d)|[;\n)]")

# Sentence boundary for scoping a context word's authority. Same decimal-point caveat.
#
# A period inside an abbreviation is NOT a sentence end. A common pay-transparency
# template reads "…base salary range for this role in the U.S. is: $105,000 — $206,000
# USD"; a bare `\.` splits "U.S." into fragments and severs the keyword from the amount,
# so the sentence-scoped gate returns None for real pay. The negative lookbehind skips a
# period preceded by a single letter (`U.`, `S.`, `e.g.`), while a period after a whole
# word ("…services.") still terminates — so the adjacent-benefit guard this gate exists
# for holds.
_SENTENCE_END_PATTERN = re.compile(r"(?<![^A-Za-z][A-Za-z])\.(?!\d)|[;\n•]")


def _cadence_for_match(text: str, match: "re.Match[str]") -> float:
    """Cadence multiplier for a single number, from the text immediately after it.

    Hourly is tested before monthly: a string carrying both ("$30/hour, billed monthly")
    is hourly-denominated. A document-wide scan had the opposite precedence, so a single
    "per month" anywhere in a JD rescaled every number by 12.
    """
    window = text[match.end(): match.end() + _CADENCE_LOOKAHEAD]
    clause_end = _CLAUSE_END_PATTERN.search(window)
    if clause_end:
        window = window[: clause_end.start()]
    if _HOURLY_PATTERN.search(window):
        return 2080.0
    if _MONTHLY_PATTERN.search(window):
        return 12.0
    return 1.0


# Units that make a number a quantity, not a salary, regardless of shape. Keyed on the
# number's IMMEDIATE suffix. Deliberately does NOT fire on cadence phrases ("per month",
# "/hour") — those are handled by _cadence_for_match, which annualizes them. A `$` prefix
# does not rescue a denylisted number.
_NEGATIVE_UNIT_PATTERN = re.compile(
    r"^\s*(?:%|hours?|hrs?|weeks?|days?|months?|years?|employees|customers|users|people)\b",
    re.IGNORECASE,
)

# "per month" / "/hour" — the noun belongs to a cadence, not to the number.
_CADENCE_PREFIX_PATTERN = re.compile(r"(?:per|/)\s*$", re.IGNORECASE)


def _is_denylisted_unit(text: str, match: "re.Match[str]") -> bool:
    """True when the number is a count of hours/years/customers/etc, not money."""
    tail = text[match.end():]
    if not _NEGATIVE_UNIT_PATTERN.match(tail):
        return False
    # `$8,000 per month` — "month" trails "per", so it denominates the amount, not counts it.
    leading_ws = len(tail) - len(tail.lstrip())
    return not _CADENCE_PREFIX_PATTERN.search(text[: match.end() + leading_ws])


def _has_false_positive_guard(text: str) -> bool:
    """Check if text matches false-positive guard patterns."""
    for pattern in _FALSE_POSITIVE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def derive_salary(
    salary_text: Optional[str], description_text: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Route each field to its own extractor.

    Context-exemption is a property of the FIELD, not of the function. `salary_text` is an
    explicit pay field (a dedicated "salary" box on a job board) and may be read
    context-exempt. `description_text` is prose and never may be — reading prose
    context-exempt turns any number near a `$`, a `k`, or a hyphen into a salary
    (a "10-15 hours per week" schedule becomes $20,800).

    The description is only consulted when the explicit field yields nothing: a
    `salary_text` of "competitive salary" carries no number, so the prose is still worth
    reading.
    """
    explicit = str(salary_text or "").strip()
    if explicit:
        result = extract_salary_from_explicit_text(explicit)
        if result is not None:
            return result

    prose = str(description_text or "").strip()
    if prose:
        return extract_salary_from_text(prose)
    return None


def extract_salary_from_text(text: str) -> Optional[Dict[str, Any]]:
    """Extract annual salary range from description prose.

    Implements strict rules:
    - Sanitizes 401k/403b patterns to avoid phantom extraction
    - Requires salary-context words, scoped to their own sentence
    - Requires shape tokens: $ prefix, k-suffix, or explicit range partner
    - Bounds: 20k-900k; outliers discarded
    - Context-less text yields None (safe fallback)

    Args:
        text: The text to extract from (typically a job description).

    Returns:
        Dict with keys: {min, max, currency, source}
        - min/max: Optional[float], None if extraction fails
        - currency: always 'USD'
        - source: always 'text_extracted'
        Example: {"min": 80000.0, "max": 120000.0, "currency": "USD", "source": "text_extracted"}
        Returns None if extraction fails entirely.
    """
    if not text or not isinstance(text, str):
        return None

    text = text.strip()
    if not text:
        return None

    # Descriptions frequently arrive HTML-entity-escaped; the context window cannot see
    # across `&lt;/span&gt;&lt;span&gt;`, so clean markup before anything else.
    text = _clean_markup(text)

    # Sanitize 401k/403b patterns first: "401k", "401(k)", "403b", "403(b)"
    text_sanitized = re.sub(r"40[13]\s*\(?\s*[kb]\s*\)?", " ", text, flags=re.IGNORECASE)

    # Find salary-context matches; if found, extract from windows around them
    context_matches = list(_CONTEXT_PATTERN.finditer(text_sanitized))

    if not context_matches:
        # No context words: description prose yields NOTHING (safe fallback)
        return None

    # Extract numbers from ~80 char windows around each context match, clipped to the
    # sentence the context word lives in. Proximity alone is not context: in
    # "…up to $20k in fertility services. The base salary range is $163,400 - $245,000"
    # the benefit sits 40 chars from "salary", so an unclipped window adopts it as the
    # floor. A salary keyword licenses numbers in ITS sentence, not the previous one.
    window_size = 80
    values = []
    for match in context_matches:
        start = max(0, match.start() - window_size)
        end = min(len(text_sanitized), match.end() + window_size)
        window = text_sanitized[start:end]
        rel_start = match.start() - start

        left = list(_SENTENCE_END_PATTERN.finditer(window, 0, rel_start))
        if left:
            window = window[left[-1].end():]
            rel_start -= left[-1].end()
        right = _SENTENCE_END_PATTERN.search(window, rel_start)
        if right:
            window = window[: right.start()]

        # Gate per WINDOW, not per document: a US posting whose JD mentions "€" once in an
        # unrelated benefits sentence must keep its dollar salary. Only the sentence that
        # actually carries the number decides its currency.
        if _is_foreign_currency(window):
            continue

        values.extend(_extract_values(window, annualize=True))

    values = sorted(set(values))  # deduplicate
    if not values:
        return None

    # Build result
    if len(values) >= 2:
        return {
            "min": values[0],
            "max": values[-1],
            "currency": "USD",
            "source": "text_extracted",
        }
    else:
        return {
            "min": values[0],
            "max": None,
            "currency": "USD",
            "source": "text_extracted",
        }


def extract_salary_from_explicit_text(text: str) -> Optional[Dict[str, Any]]:
    """Extract salary from an explicit salary field (context-exempt).

    Pipeline (in correct order):
    1. Strip HTML tags
    2. Check false-positive guards (reject unless salary context present)
    3. Sanitize 401k/403b patterns
    4. Detect cadence (monthly/hourly)
    5. Extract raw numbers using shape gates
    6. Normalize to annual (multiply by cadence)
    7. Apply bounds (20k-900k annual)
    8. Detect currency — a non-USD field yields NO salary (see `_FOREIGN_CURRENCY`)
    """
    # An explicit salary field is short and dedicated: any foreign marker anywhere in it
    # means the amount is not USD. A wrong number in a USD result silently corrupts
    # downstream filtering, whereas None is honest.
    if _is_foreign_currency(text or ""):
        return None
    if not text or not isinstance(text, str):
        return None

    text = text.strip()
    if not text:
        return None

    # 1. Un-escape entities, then strip tags
    text = _clean_markup(text)

    # 2. Check false-positive guards (reject unless salary context present)
    if _has_false_positive_guard(text):
        if not _CONTEXT_PATTERN.search(text):
            return None

    # 3. Sanitize 401k/403b patterns
    text_sanitized = re.sub(r"40[13]\s*\(?\s*[kb]\s*\)?", " ", text, flags=re.IGNORECASE)

    # 4-7. Shape-gate + denylist + per-number cadence, then bounds on the annualized value.
    normalized_values = sorted(set(_extract_values(text_sanitized, annualize=True)))
    if not normalized_values:
        return None

    # 8. Detect currency
    currency = _detect_currency(text_sanitized)

    # Return result
    if len(normalized_values) >= 2:
        return {
            "min": normalized_values[0],
            "max": normalized_values[-1],
            "currency": currency,
            "source": "text_extracted",
        }
    else:
        return {
            "min": normalized_values[0],
            "max": None,
            "currency": currency,
            "source": "text_extracted",
        }


_MIN_ANNUAL = 20000
_MAX_ANNUAL = 900000

# Anchored range partner: the current number must be an operand. Applied to the text
# immediately before / after the match, not to a window that merely contains a range.
# `—` (em dash, U+2014) is REQUIRED: Greenhouse's pay-transparency block renders
# `$72,000 &mdash; $91,000`, which un-escapes to an em dash. Without it the right operand
# is unreachable and the max is silently dropped. `−` is the Unicode minus sign (U+2212),
# which some ATSes emit instead of a hyphen.
_RANGE_DASHES = "\\-–—−"
_RANGE_LEFT_PATTERN = re.compile(rf"(?:[{_RANGE_DASHES}]|\bto)\s*\$?\s*$", re.IGNORECASE)
_RANGE_RIGHT_PATTERN = re.compile(rf"^\s*(?:[{_RANGE_DASHES}]|to\b)\s*\$?\s*[\d,]", re.IGNORECASE)


def _has_anchored_range_partner(text: str, match: "re.Match[str]") -> bool:
    """True when this match is the left or right operand of an explicit range.

    A naive check that searches a ±20-char window for *any* `\\d+\\s*[-–to]\\s*\\d+` lets
    the `10` in "10-15 hours per week" qualify on a range it isn't part of.
    """
    before = text[max(0, match.start() - 20): match.start()]
    after = text[match.end(): match.end() + 20]
    # Right operand: something-dash-<me>.  Left operand: <me>-dash-something.
    return bool(_RANGE_LEFT_PATTERN.search(before) or _RANGE_RIGHT_PATTERN.match(after))


def _extract_values(text: str, *, annualize: bool) -> list[float]:
    """Shape-gate, denylist, per-number cadence, then bounds. The one extraction core.

    `annualize` controls whether a number's trailing cadence ("/hour", "per month") is
    honored. Bounds are always applied *after* the multiplier, so an hourly rate is
    range-checked as its annual equivalent, never as a bare `45`.

    A denylisted operand neuters its whole range: "10-15 hours" must drop the `10` too,
    otherwise it survives on `15`'s coattails.
    """
    candidates: list[tuple[float, "re.Match[str]", bool]] = []
    for match in _SHAPE_PATTERN.finditer(text):
        try:
            amount = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        if match.group(2):  # k-suffix
            amount *= 1000.0
        candidates.append((amount, match, _is_denylisted_unit(text, match)))

    # Propagate a denylisted operand across its range partners.
    poisoned = {
        i
        for i, (_, m, bad) in enumerate(candidates)
        if bad and _has_anchored_range_partner(text, m)
    }
    for i in list(poisoned):
        for j, (_, m2, _) in enumerate(candidates):
            if j != i and abs(m2.start() - candidates[i][1].start()) <= 20:
                poisoned.add(j)

    values: list[float] = []
    for i, (amount, match, denylisted) in enumerate(candidates):
        if denylisted or i in poisoned:
            continue

        full_match = match.group(0)
        has_currency = any(
            c in full_match or (match.start() > 0 and text[match.start() - 1] == c)
            for c in "$£€¥₹"
        )
        has_k = bool(match.group(2))
        if not (has_currency or has_k or _has_anchored_range_partner(text, match)):
            continue

        normalized = amount * (_cadence_for_match(text, match) if annualize else 1.0)
        if _MIN_ANNUAL <= normalized <= _MAX_ANNUAL:
            values.append(normalized)

    return values
