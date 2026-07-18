"""Tests for salary text extraction.

Validates strict compensation extraction rules:
- 401k/403b sanitization
- Context window behavior
- Shape-token gating ($ / k / range)
- Bounds enforcement (20k-900k)
- Context-less prose yields nothing
- Explicit salary text is context-exempt

Many cases below are regressions taken from real job postings that a naive
extractor misread — they are the reason each rule exists.
"""

import pytest
from salary_parser import (
    extract_salary_from_text,
    extract_salary_from_explicit_text,
)


class TestContextedExtraction:
    """Tests for extract_salary_from_text (context-required)."""

    def test_empty_input(self):
        """Empty/None input yields None."""
        assert extract_salary_from_text("") is None
        assert extract_salary_from_text(None) is None
        assert extract_salary_from_text("   ") is None

    def test_basic_salary_with_context_word(self):
        """Salary in context window with $ prefix extracts."""
        result = extract_salary_from_text("Salary: $80,000")
        assert result is not None
        assert result["min"] == 80000.0
        assert result["max"] is None
        assert result["currency"] == "USD"
        assert result["source"] == "text_extracted"

    def test_salary_range_with_context(self):
        """Salary range with context word extracts both bounds."""
        result = extract_salary_from_text("Annual salary: $80,000 to $120,000")
        assert result is not None
        assert result["min"] == 80000.0
        assert result["max"] == 120000.0

    def test_salary_with_k_suffix(self):
        """Number with k-suffix within context window extracts."""
        result = extract_salary_from_text("Compensation: 80k-120k")
        assert result is not None
        assert result["min"] == 80000.0
        assert result["max"] == 120000.0

    def test_salary_range_dash_variant(self):
        """Range with dash/hyphen extracts."""
        result = extract_salary_from_text("Compensation: $100,000 - $150,000")
        assert result is not None
        assert result["min"] == 100000.0
        assert result["max"] == 150000.0

    def test_multiple_context_words(self):
        """Multiple context words allow extraction from multiple windows."""
        text = "Base salary: $80k. We also offer annual compensation around $100,000."
        result = extract_salary_from_text(text)
        assert result is not None
        # Should extract values from both windows
        assert result["min"] in [80000.0, 100000.0]
        assert result["max"] in [80000.0, 100000.0]
        assert result["min"] <= result["max"] if result["max"] else True

    def test_401k_sanitization(self):
        """401k patterns are sanitized before extraction."""
        # Without sanitization, "401k" could be extracted as $401k
        result = extract_salary_from_text("Salary: $80,000. 401(k) match available.")
        assert result is not None
        assert result["min"] == 80000.0
        # Should NOT extract the 401k as salary
        if result["max"]:
            assert result["max"] != 401000.0

    def test_401k_variations_all_sanitized(self):
        """All 401k/403b variations are sanitized."""
        for pattern in ["401k", "401(k)", "403b", "403(b)"]:
            text = f"Salary: $90,000. {pattern} available."
            result = extract_salary_from_text(text)
            assert result is not None
            assert result["min"] == 90000.0
            # 401/403 phantom should NOT be extracted
            if result["max"]:
                assert 400000 <= result["max"] <= 410000 is False

    def test_context_window_80_chars(self):
        """Number within ~80 char window of context word extracts."""
        text = "Salary:" + " " * 70 + "$80,000"
        result = extract_salary_from_text(text)
        assert result is not None
        assert result["min"] == 80000.0

    def test_number_outside_context_window_no_extract(self):
        """Number > 80 chars away from context word does not extract."""
        text = "Salary:" + " " * 100 + "$80,000"
        result = extract_salary_from_text(text)
        assert result is None

    def test_context_less_prose_yields_nothing(self):
        """Description prose without context words yields nothing."""
        result = extract_salary_from_text("Sign-on bonus of $50,000. Benefits include health insurance.")
        assert result is None

    def test_context_less_large_number_no_extract(self):
        """Large number without context word and no shape tokens does not extract."""
        result = extract_salary_from_text("We have 150,000 employees worldwide.")
        assert result is None

    def test_bare_number_without_shape_tokens(self):
        """Bare number like 70,000 without $ or k does not extract without context."""
        result = extract_salary_from_text("The position involves 70,000 hours of work per year.")
        # "hours" is not a salary context word
        assert result is None

    def test_ote_context_word(self):
        """OTE (on-target earnings) is recognized as context."""
        result = extract_salary_from_text("OTE: $100k")
        assert result is not None
        assert result["min"] == 100000.0

    def test_annual_context_word(self):
        """Annual is recognized as context."""
        result = extract_salary_from_text("Annual compensation: $95000")
        assert result is not None
        assert result["min"] == 95000.0

    def test_bare_number_with_annual_context_no_extract(self):
        """Bare number with annual context but no shape tokens does not extract."""
        result = extract_salary_from_text("Annual compensation: 95000")
        assert result is None

    def test_annually_context_word(self):
        """Annually is recognized as context."""
        result = extract_salary_from_text("Pay: $120,000 annually")
        assert result is not None
        assert result["min"] == 120000.0

    def test_base_salary_context(self):
        """Base salary is recognized as context."""
        result = extract_salary_from_text("Base salary of $110,000")
        assert result is not None
        assert result["min"] == 110000.0

    def test_out_of_bounds_low(self):
        """Numbers below 20k are discarded."""
        result = extract_salary_from_text("Salary: $10,000")
        assert result is None

    def test_out_of_bounds_high(self):
        """Numbers above 900k are discarded."""
        result = extract_salary_from_text("Salary: $1,000,000")
        assert result is None

    def test_bounds_at_edges(self):
        """Bounds at exact edges (20k, 900k) are accepted."""
        result_low = extract_salary_from_text("Salary: $20,000")
        assert result_low is not None
        assert result_low["min"] == 20000.0

        result_high = extract_salary_from_text("Salary: $900,000")
        assert result_high is not None
        assert result_high["min"] == 900000.0

    def test_mixed_in_out_of_bounds(self):
        """Out-of-bounds values are discarded, in-bounds kept."""
        result = extract_salary_from_text("Salary ranges: $10,000 to $80,000 to $1,000,000")
        assert result is not None
        assert result["min"] == 80000.0

    def test_deduplication(self):
        """Duplicate values are deduplicated."""
        result = extract_salary_from_text("Salary: $80,000 and compensation around $80,000")
        assert result is not None
        assert result["min"] == 80000.0

    def test_range_implicit_from_context(self):
        """Range from dash/hyphen/to is recognized."""
        result = extract_salary_from_text("Compensation: 100k - 150k")
        assert result is not None
        assert result["min"] == 100000.0
        assert result["max"] == 150000.0

    def test_single_value_when_one_number(self):
        """Single value yields min only, max=None."""
        result = extract_salary_from_text("Salary: $85,000")
        assert result is not None
        assert result["min"] == 85000.0
        assert result["max"] is None

    def test_return_format(self):
        """Return format has required keys."""
        result = extract_salary_from_text("Salary: $80,000")
        assert result is not None
        assert set(result.keys()) == {"min", "max", "currency", "source"}
        assert result["currency"] == "USD"
        assert result["source"] == "text_extracted"


class TestExplicitTextExtraction:
    """Tests for extract_salary_from_explicit_text (context-exempt)."""

    def test_explicit_bare_number_extracts(self):
        """$-prefixed number in explicit salary text extracts (context-exempt)."""
        result = extract_salary_from_explicit_text("$80,000")
        assert result is not None
        assert result["min"] == 80000.0

    def test_explicit_no_context_needed(self):
        """Explicit salary text extracts without context words."""
        result = extract_salary_from_explicit_text("80k-120k")
        assert result is not None
        assert result["min"] == 80000.0
        assert result["max"] == 120000.0

    def test_explicit_401k_still_sanitized(self):
        """401k patterns are still sanitized in explicit text."""
        result = extract_salary_from_explicit_text("401k match; $100,000")
        assert result is not None
        assert result["min"] == 100000.0
        if result["max"]:
            assert 400000 <= result["max"] <= 410000 is False

    def test_explicit_empty_input(self):
        """Empty input yields None."""
        assert extract_salary_from_explicit_text("") is None
        assert extract_salary_from_explicit_text(None) is None

    def test_explicit_out_of_bounds_still_enforced(self):
        """Bounds are still enforced even for explicit text."""
        result = extract_salary_from_explicit_text("$1,000,000")
        assert result is None


class TestEdgeCases:
    """Edge cases and real-world scenarios."""

    def test_comma_separated_numbers(self):
        """Numbers with commas parse correctly."""
        result = extract_salary_from_text("Salary: $150,000")
        assert result is not None
        assert result["min"] == 150000.0

    def test_decimal_numbers(self):
        """A lone hourly rate below annual bounds without cadence context is discarded."""
        result = extract_salary_from_text("Hourly rate: $45.50")
        # $45.50 with no trailing cadence phrase stays 45.50, which is out of bounds
        assert result is None

    def test_no_dollar_sign_with_k(self):
        """k-suffix without $ still gates extraction."""
        result = extract_salary_from_text("Salary: 80k")
        assert result is not None
        assert result["min"] == 80000.0

    def test_en_dash_range(self):
        """En-dash (–) in ranges is recognized."""
        result = extract_salary_from_text("Compensation: $100,000–$150,000")
        assert result is not None
        assert result["min"] == 100000.0
        assert result["max"] == 150000.0

    def test_whitespace_handling(self):
        """Extra whitespace is handled."""
        result = extract_salary_from_text("Salary:   $80,000")
        assert result is not None
        assert result["min"] == 80000.0

    def test_case_insensitivity(self):
        """Salary context words are case-insensitive."""
        result = extract_salary_from_text("SALARY: $80,000")
        assert result is not None
        assert result["min"] == 80000.0

        result = extract_salary_from_text("Salary: $80K")
        assert result is not None
        assert result["min"] == 80000.0

    def test_multiline_text(self):
        """Multiline text is handled (context window spans lines)."""
        text = """The position offers:
        Salary: $85,000
        Benefits: health insurance
        """
        result = extract_salary_from_text(text)
        assert result is not None
        assert result["min"] == 85000.0

    def test_real_world_jd_snippet_1(self):
        """Real JD snippet with multiple numbers."""
        text = """
        Base Salary: $75,000 - $100,000
        We have 5,000 employees in the US.
        Annual bonus up to 15% of salary.
        """
        result = extract_salary_from_text(text)
        assert result is not None
        assert result["min"] == 75000.0
        assert result["max"] == 100000.0

    def test_real_world_jd_snippet_2(self):
        """Real JD snippet with 401k near salary."""
        text = """
        Compensation: $120,000 annually
        Benefits: 401(k) matching (up to 6%)
        """
        result = extract_salary_from_text(text)
        assert result is not None
        assert result["min"] == 120000.0

    def test_real_world_jd_snippet_3(self):
        """Real JD snippet where sign-on bonus is NOT extracted (no context)."""
        text = """
        Base Salary: $95,000
        Sign-on bonus: $15,000
        """
        result = extract_salary_from_text(text)
        assert result is not None
        assert result["min"] == 95000.0

    def test_generic_large_number_safe(self):
        """Generic large numbers without context are safe (no extraction)."""
        text = "Our company generated $500,000,000 in revenue last year."
        result = extract_salary_from_text(text)
        assert result is None

    def test_range_without_comma_k_suffix(self):
        """Range like 80k - 120k without commas extracts."""
        result = extract_salary_from_text("Salary: 80k - 120k")
        assert result is not None
        assert result["min"] == 80000.0
        assert result["max"] == 120000.0


class TestStrictModeRegressions:
    """The extractor must never fall back to shape-only extraction on prose."""

    def test_no_fallback_to_shape_only_full_text(self):
        """Strict mode does NOT fall back to shape-only extraction without context."""
        text = "This is description text with number 80,000 but no salary context."
        result = extract_salary_from_text(text)
        assert result is None

    def test_explicit_text_has_different_rules(self):
        """Explicit salary text uses context-exempt extraction — but still needs shape."""
        result = extract_salary_from_explicit_text("80,000")
        # Bare "80,000" without $ or k has no shape token
        assert result is None

        result = extract_salary_from_explicit_text("$80,000")
        assert result is not None
        assert result["min"] == 80000.0


class TestPerNumberCadence:
    """Cadence belongs to the number it trails, not to the document.

    A document-wide cadence scan means one incidental `/hour` or `per month`
    anywhere in a JD rescales every number in it.
    """

    def test_incidental_hourly_does_not_rescale_annual_range(self):
        result = extract_salary_from_explicit_text(
            "$60,000-$80,000 annually (approximately $30/hour)"
        )
        assert (result["min"], result["max"]) == (60000.0, 80000.0)

    def test_incidental_monthly_stipend_does_not_rescale_annual_range(self):
        result = extract_salary_from_explicit_text(
            "Base salary $120,000-$150,000 per year. Monthly stipend of $2,000."
        )
        assert (result["min"], result["max"]) == (120000.0, 150000.0)

    def test_hourly_range_annualizes_both_operands(self):
        result = extract_salary_from_explicit_text("$45.00 - $55.00 per hour")
        assert (result["min"], result["max"]) == (93600.0, 114400.0)

    def test_monthly_salary_still_annualizes(self):
        """The quantity denylist must not neuter a legitimate monthly cadence."""
        result = extract_salary_from_explicit_text("$8,000 per month")
        assert result["min"] == 96000.0

    def test_slash_hour_matches_when_number_is_adjacent(self):
        """`\\b/hour` never matched at a window start; `$23/hour` was silently annual."""
        result = extract_salary_from_explicit_text("$22-$23/hour")
        assert (result["min"], result["max"]) == (45760.0, 47840.0)


class TestNegativeUnitDenylist:
    """A number counting hours/years/customers/percent is not a salary."""

    @pytest.mark.parametrize(
        "text",
        [
            "10-15 hours per week",
            "5-7 years of experience",
            "serve 100,000 - 250,000 customers",
            "a team of 50,000 - 90,000 employees",
        ],
    )
    def test_quantities_yield_nothing(self, text):
        assert extract_salary_from_explicit_text(text) is None

    def test_denylisted_operand_neuters_its_range_partner(self):
        """`10` must die with `15`, not survive on its range partner's coattails."""
        result = extract_salary_from_explicit_text(
            "Schedule: 10-15 hours per week. Hourly Compensation: $22-$23/hour"
        )
        assert (result["min"], result["max"]) == (45760.0, 47840.0)

    def test_percent_neutered_but_dollar_amount_survives(self):
        result = extract_salary_from_explicit_text("$120,000 + 10% bonus")
        assert result["min"] == 120000.0

    def test_bare_range_in_salary_text_still_extracts(self):
        """Negative guard: tightening the shape gate must not suppress real bare ranges."""
        result = extract_salary_from_explicit_text("140,000 - 180,000")
        assert (result["min"], result["max"]) == (140000.0, 180000.0)


class TestRangePartnerAnchoring:
    """The match must BE an operand of the range, not merely sit near one."""

    def test_unrelated_range_does_not_confer_shape(self):
        # `250,000` has no $/k and is not an operand of `1-4`.
        assert extract_salary_from_explicit_text("1-4 projects, 250,000 lines") is None

    def test_left_operand_accepted(self):
        result = extract_salary_from_explicit_text("140,000 to 180,000")
        assert result["min"] == 140000.0


class TestProseIsNeverContextExempt:
    """Description prose must always go through the context-gated extractor."""

    @pytest.mark.parametrize(
        "prose",
        [
            "You will manage a budget of $250,000",
            "Our platform processed $85,000 - $120,000 in transactions",
        ],
    )
    def test_context_less_prose_yields_nothing(self, prose):
        assert extract_salary_from_text(prose) is None

    def test_gated_path_annualizes_hourly_prose(self):
        result = extract_salary_from_text(
            "Schedule: 10-15 hours per week. Hourly Compensation: $22-$23/hour"
        )
        assert (result["min"], result["max"]) == (45760.0, 47840.0)


class TestRealWorldRegressions:
    """Regressions taken from real postings that a naive extractor misread."""

    def test_commission_range_not_rescaled_by_saturdays_per_month(self):
        # A naive extractor rescaled $50,000 by 12 from "two Saturdays per month".
        result = extract_salary_from_explicit_text(
            "Schedule: Monday-Friday, plus two Saturdays per month from April-October. "
            "Compensation and Benefits Earning potential of $50,000-$80,000 "
            "(commission based structure with no upper limit) 401K Plan with Company Match"
        )
        assert (result["min"], result["max"]) == (50000.0, 80000.0)

    def test_contract_months_not_treated_as_hourly_rate(self):
        # A naive extractor read `18` from "(6-18 months)" and multiplied by 2080.
        result = extract_salary_from_explicit_text(
            "Our Embedded practice supports long-term contract opportunities "
            "(6-18 months) with some of our largest clients. Freelance - $70-$100/hr (1099)"
        )
        assert (result["min"], result["max"]) == (145600.0, 208000.0)

    def test_travel_percentage_is_not_a_salary_ceiling(self):
        # A naive extractor read `80` from "Travel 80%" and multiplied by 2080.
        result = extract_salary_from_explicit_text(
            "Job requires regular travel within the region. Travel 80% "
            "Pay: $45- $50/hour The pay listed is the hourly range or the hourly rate "
            "for this position."
        )
        assert (result["min"], result["max"]) == (93600.0, 104000.0)

    def test_correct_hourly_rows_are_unchanged(self):
        result = extract_salary_from_explicit_text(
            "Pay rate:  $20.00 hourly Hours/Availability: The schedule will range "
            "anywhere from 5 hrs to 30 hrs per week,  between 6-8-hour shifts per day"
        )
        assert result["min"] == 41600.0

    def test_correct_hourly_range_rows_are_unchanged(self):
        result = extract_salary_from_explicit_text(
            "Compensation: $19 to $20 per hour Schedule: Thursday to Monday; 3:00 PM to 11:30 PM"
        )
        assert (result["min"], result["max"]) == (39520.0, 41600.0)


class TestCadenceIsSalaryContext:
    """A pay cadence licenses the gated (prose) extractor.

    Without cadence words in the context vocabulary, a perfectly explicit
    "Pay rate: $20.00 hourly" yields nothing from the prose path.
    """

    def test_pay_rate_hourly(self):
        result = extract_salary_from_text("Pay rate:  $20.00 hourly")
        assert result["min"] == 41600.0

    def test_pay_colon_slash_hour(self):
        result = extract_salary_from_text("Pay: $45- $50/hour")
        assert (result["min"], result["max"]) == (93600.0, 104000.0)

    def test_wage_is_context(self):
        result = extract_salary_from_text("Starting wage of $65,000")
        assert result["min"] == 65000.0

    def test_cadence_context_does_not_resurrect_pure_prose(self):
        """Widening the vocabulary must not re-open the fabrication door."""
        assert extract_salary_from_text("You will manage a budget of $250,000") is None
        assert extract_salary_from_text("processed $85,000 - $120,000 in transactions") is None

    def test_hours_per_week_still_yields_nothing(self):
        assert extract_salary_from_text("Schedule: 10-15 hours per week") is None


class TestHtmlEntityEscapedText:
    """Descriptions are frequently stored HTML-ENTITY-escaped, not as real markup.

    A tag-stripper alone never fires on `&lt;div&gt;` soup, so the context window
    can't reach across it — every Greenhouse-style pay-transparency block would
    extract nothing.
    """

    def test_entity_escaped_pay_range_block(self):
        raw = (
            "&lt;div&gt;&lt;p&gt;Position Pay Range&lt;/p&gt;&lt;span&gt;$72,000&lt;/span&gt;"
            "&lt;span&gt;&amp;mdash;&lt;/span&gt;&lt;span&gt;$91,000 USD&lt;/span&gt;&lt;/div&gt;"
        )
        result = extract_salary_from_text(raw)
        assert (result["min"], result["max"]) == (72000.0, 91000.0)

    def test_entities_are_unescaped_to_a_fixed_point(self):
        """`&amp;mdash;` needs TWO passes: one yields the literal text `&mdash;`."""
        from salary_parser import _clean_markup

        assert "—" in _clean_markup("&amp;mdash;")
        assert "<" not in _clean_markup("&lt;div&gt;")

    def test_tags_become_spaces_not_nothing(self):
        """`$72,000</span><span>$91,000` must not glue into `$72,000$91,000`."""
        result = extract_salary_from_text(
            "&lt;span&gt;$72,000&lt;/span&gt;&lt;span&gt;$91,000&lt;/span&gt; base salary"
        )
        assert (result["min"], result["max"]) == (72000.0, 91000.0)

    def test_nbsp_is_normalised_to_a_space(self):
        """`&nbsp;` un-escapes to U+00A0, which `re` does not treat as `\\s`."""
        result = extract_salary_from_text("Pay&nbsp;Range&nbsp;$80,000&nbsp;-&nbsp;$90,000")
        assert (result["min"], result["max"]) == (80000.0, 90000.0)

    def test_cleaning_markup_does_not_resurrect_fabrications(self):
        """Markup cleaning must not weaken the context gate."""
        assert extract_salary_from_text("&lt;p&gt;You will manage a budget of $250,000&lt;/p&gt;") is None
        assert extract_salary_from_text(
            "&lt;p&gt;processed $85,000 - $120,000 in transactions&lt;/p&gt;"
        ) is None


class TestRangeSeparatorDashes:
    """Some ATSes render `$72,000 &mdash; $91,000`. An em dash is a range separator."""

    @pytest.mark.parametrize(
        "sep", ["-", "–", "—", "−"],  # hyphen, en dash, em dash, minus sign
    )
    def test_every_dash_joins_a_range(self, sep):
        result = extract_salary_from_text(f"Pay Range $140,000 {sep} $176,000 USD")
        assert (result["min"], result["max"]) == (140000.0, 176000.0), sep

    def test_missing_em_dash_used_to_drop_the_max(self):
        """Regression: without em-dash support the max is silently dropped."""
        result = extract_salary_from_text("Position Pay Range $72,000 — $91,000 USD")
        assert result["max"] == 91000.0


class TestAbbreviationPeriodDoesNotSeverSentence:
    """A period inside an abbreviation ("U.S.") is not a sentence boundary.

    The sentence-scoped gate licenses a number only in the salary keyword's own
    sentence, so a benefit amount in an ADJACENT sentence is not adopted as the
    floor. But treating every non-decimal `.` as a terminator splits "U.S." into
    fragments and severs "base salary range" from "$105,000" in a very common
    pay-transparency template.
    """

    def test_us_abbreviation_pay_block(self):
        result = extract_salary_from_text(
            "Please note, the base salary range listed below and the benefits in this paragraph "
            "are only applicable to U.S.-based candidates. The company's base salary range for this "
            "role in the U.S. is: $105,000 — $206,000 USD"
        )
        assert (result["min"], result["max"]) == (105000.0, 206000.0)

    def test_single_value_us_block(self):
        # "$150,000 — $150,000" collapses to one value.
        result = extract_salary_from_text(
            "The company's base salary range for this role in the U.S. is: $150,000 — $150,000 USD"
        )
        assert result["min"] == 150000.0

    def test_adjacent_sentence_benefit_is_still_not_adopted(self):
        """The guard this whole gate exists for must not regress: a benefit amount in a real
        neighbouring sentence stays excluded; only the keyword sentence's range is read."""
        result = extract_salary_from_text(
            "We offer up to $20k in fertility services. The base salary range is "
            "$163,400 - $245,000"
        )
        assert (result["min"], result["max"]) == (163400.0, 245000.0)

    def test_real_period_still_terminates_a_sentence(self):
        """A period after a whole word remains a boundary — a stipend one sentence away from a
        salary keyword must not be pulled in."""
        assert extract_salary_from_text(
            "The role includes a signing stipend of $30,000. Relocation is negotiable."
        ) is None
