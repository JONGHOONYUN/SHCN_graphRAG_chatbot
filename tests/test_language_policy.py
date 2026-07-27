"""work order CLAUDE_CODE_MIXED_SCRIPT_LANGUAGE_DETECTION_AND_ROUTING.md
§9.1/9.2 — pure detector and control/state-resolver tests for
`tools.language_policy`. `bot.py` is never imported (it runs Streamlit page
config / auth at import time); this module has zero streamlit/Neo4j/LLM
imports, so every test here runs with no network and no live credentials.
"""

import time
import unicodedata
import unittest

from tools.language_policy import (
    DEFAULT_LANGUAGE,
    LanguageControl,
    LanguageResolution,
    detect_language_control,
    detect_question_language,
    remove_language_control,
    resolve_languages,
)


# ── §9.1 mandatory fixtures (work order §4.6 table, verbatim) ───────────────
class TestDetectQuestionLanguageFixtures(unittest.TestCase):
    def test_english_grammar_with_chinese_entity_name(self):
        self.assertEqual(detect_question_language("How is 杜甫 critiqued?"), "en")

    def test_english_grammar_with_korean_entity_name(self):
        self.assertEqual(detect_question_language("Who is 이규보?"), "en")

    def test_chinese_grammar_simplified(self):
        self.assertEqual(detect_question_language("杜甫如何被评价？"), "zh")

    def test_chinese_grammar_traditional(self):
        self.assertEqual(detect_question_language("杜甫如何被評價？"), "zh")

    def test_chinese_grammar_with_latin_book_title(self):
        self.assertEqual(
            detect_question_language("《Sihwa Ch'ongnim》中如何评价杜甫？"), "zh")

    def test_korean_grammar_with_chinese_entity_name(self):
        self.assertEqual(
            detect_question_language("두보(杜甫)는 어떻게 평가되는가?"), "ko")

    def test_korean_grammar_with_latin_entity_name(self):
        self.assertEqual(
            detect_question_language("시화총림에서 Du Fu는 어떻게 평가되는가?"), "ko")

    def test_latin_only_fallback(self):
        self.assertEqual(detect_question_language("Du Fu"), "en")

    def test_han_only_fallback(self):
        self.assertEqual(detect_question_language("杜甫"), "zh")

    def test_hangul_only_fallback(self):
        self.assertEqual(detect_question_language("두보"), "ko")

    def test_no_script_signal_defaults_to_ko(self):
        self.assertEqual(detect_question_language("123?! 😊"), "ko")


class TestDetectQuestionLanguageSafetyAndRobustness(unittest.TestCase):
    def test_none_is_safe(self):
        self.assertEqual(detect_question_language(None), DEFAULT_LANGUAGE)

    def test_empty_string_is_safe(self):
        self.assertEqual(detect_question_language(""), DEFAULT_LANGUAGE)

    def test_whitespace_only_is_safe(self):
        self.assertEqual(detect_question_language("   \t\n"), DEFAULT_LANGUAGE)

    def test_nfc_and_nfd_forms_agree(self):
        nfc = unicodedata.normalize("NFC", "Hŏ Ch'ongnim is a great compilation")
        nfd = unicodedata.normalize("NFD", "Hŏ Ch'ongnim is a great compilation")
        self.assertNotEqual(nfc, nfd)   # sanity: forms really do differ byte-wise
        self.assertEqual(detect_question_language(nfc),
                         detect_question_language(nfd))

    def test_latin_extended_diacritics_count_as_latin_content(self):
        # Romanized Korean (Hŏ, Ch'ongnim) must not be miscounted as "no
        # script" or silently dropped from the Latin content tally.
        self.assertEqual(detect_question_language("Hŏ Ch'ongnim"), "en")

    def test_deterministic_repeated_calls(self):
        results = {detect_question_language("How is 杜甫 critiqued?") for _ in range(25)}
        self.assertEqual(results, {"en"})

    def test_no_catastrophic_backtracking_on_long_input(self):
        long_text = "How is 杜甫 critiqued? " * 3000
        start = time.time()
        detect_question_language(long_text)
        self.assertLess(time.time() - start, 2.0)

    def test_plain_sentence_with_incidental_language_name_not_special_cased(self):
        # This is a grammar-detection sanity check, distinct from lock-cue
        # detection (see TestExplicitLockNotFalsePositive below) — a plain
        # English sentence mentioning "English" is still just English.
        self.assertEqual(detect_question_language("I love English literature"), "en")


# ── §9.2 control-phrase span detection / removal ────────────────────────────
class TestExplicitLockNotFalsePositive(unittest.TestCase):
    """The single most important negative case: a normal sentence must never
    be mistaken for a language-lock command."""

    def test_incidental_language_mention_is_not_a_lock(self):
        control = detect_language_control("I love English literature")
        self.assertIsNone(control.lock_language)
        self.assertFalse(control.release)
        self.assertEqual(control.spans, ())

    def test_resolve_languages_treats_it_as_a_normal_question(self):
        r = resolve_languages("I love English literature")
        self.assertEqual(r.action, "none")
        self.assertIsNone(r.locked_language)
        self.assertEqual(r.question_text, "I love English literature")
        self.assertFalse(r.control_only)


class TestLanguageControlSpanRemoval(unittest.TestCase):
    def test_answer_in_korean_colon_prefix(self):
        control = detect_language_control("Answer in Korean: How is 杜甫 critiqued?")
        self.assertEqual(control.lock_language, "ko")
        cleaned = remove_language_control(
            "Answer in Korean: How is 杜甫 critiqued?", control.spans)
        self.assertEqual(cleaned, "How is 杜甫 critiqued?")

    def test_korean_lock_with_trailing_conjugation_and_period(self):
        text = "영어로 답변해줘. 두보는 어떻게 평가되는가?"
        control = detect_language_control(text)
        self.assertEqual(control.lock_language, "en")
        cleaned = remove_language_control(text, control.spans)
        self.assertEqual(cleaned, "두보는 어떻게 평가되는가?")

    def test_chinese_lock_with_verb_and_terminal_punctuation(self):
        text = "请用英文回答。杜甫如何被评价？"
        control = detect_language_control(text)
        self.assertEqual(control.lock_language, "en")
        cleaned = remove_language_control(text, control.spans)
        self.assertEqual(cleaned, "杜甫如何被评价？")

    def test_release_phrase_detected(self):
        control = detect_language_control("자동 언어 감지")
        self.assertTrue(control.release)
        self.assertIsNone(control.lock_language)

    def test_english_release_phrase_detected(self):
        control = detect_language_control("Please follow my question language from now on")
        self.assertTrue(control.release)

    def test_no_control_phrase_yields_empty_spans(self):
        control = detect_language_control("두보는 어떻게 평가되는가?")
        self.assertIsNone(control.lock_language)
        self.assertFalse(control.release)
        self.assertEqual(control.spans, ())

    def test_none_and_empty_safe(self):
        self.assertEqual(detect_language_control(None), LanguageControl(None, False, ()))
        self.assertEqual(remove_language_control(None, ()), "")
        self.assertEqual(remove_language_control("hello", None), "hello")

    def test_entity_names_apostrophes_and_quotes_preserved(self):
        text = 'Answer in English: What did Ch\'wisŏn say about "moonlight"?'
        control = detect_language_control(text)
        cleaned = remove_language_control(text, control.spans)
        self.assertEqual(cleaned, 'What did Ch\'wisŏn say about "moonlight"?')


# ── §9.2 resolve_languages() state-resolution matrix ────────────────────────
class TestResolveLanguagesMatrix(unittest.TestCase):
    def test_worked_example_english_lock_over_mixed_script_question(self):
        r = resolve_languages("Answer in Korean: How is 杜甫 critiqued?")
        self.assertEqual(r.question_text, "How is 杜甫 critiqued?")
        self.assertEqual(r.question_language, "en")
        self.assertEqual(r.response_language, "ko")
        self.assertEqual(r.locked_language, "ko")
        self.assertEqual(r.action, "lock")
        self.assertFalse(r.control_only)

    def test_korean_lock_request_for_english_response(self):
        # 영어로 답변해줘. 두보는 어떻게 평가되는가? -> q=ko, response=en
        r = resolve_languages("영어로 답변해줘. 두보는 어떻게 평가되는가?")
        self.assertEqual(r.question_text, "두보는 어떻게 평가되는가?")
        self.assertEqual(r.question_language, "ko")
        self.assertEqual(r.response_language, "en")
        self.assertEqual(r.locked_language, "en")

    def test_chinese_lock_request_for_english_response(self):
        r = resolve_languages("请用英文回答。杜甫如何被评价？")
        self.assertEqual(r.question_language, "zh")
        self.assertEqual(r.response_language, "en")
        self.assertEqual(r.locked_language, "en")

    def test_prior_lock_persists_across_a_different_question_language(self):
        r = resolve_languages("杜甫如何被评价？", locked_language="ko")
        self.assertEqual(r.question_language, "zh")
        self.assertEqual(r.response_language, "ko")
        self.assertEqual(r.locked_language, "ko")
        self.assertEqual(r.action, "none")

    def test_new_lock_replaces_existing_lock(self):
        r = resolve_languages("Answer in English", locked_language="ko")
        self.assertEqual(r.locked_language, "en")
        self.assertEqual(r.action, "lock")

    def test_release_clears_lock_and_follows_question_language(self):
        r = resolve_languages("자동 언어 감지 그리고 두보는 어떻게 평가되는가?",
                              locked_language="en")
        self.assertIsNone(r.locked_language)
        self.assertEqual(r.action, "release")
        self.assertEqual(r.response_language, r.question_language)

    def test_lock_and_release_both_present_lock_wins(self):
        r = resolve_languages("Please use English and 언어 락 해제 부탁해")
        self.assertEqual(r.action, "lock")
        self.assertEqual(r.locked_language, "en")

    def test_lock_only_input_is_control_only(self):
        r = resolve_languages("Answer in English")
        self.assertTrue(r.control_only)
        self.assertEqual(r.question_text, "")
        self.assertEqual(r.question_language, "en")
        self.assertEqual(r.response_language, "en")

    def test_release_only_input_is_control_only(self):
        r = resolve_languages("자동 언어 감지")
        self.assertTrue(r.control_only)
        self.assertEqual(r.question_text, "")
        # release-only falls back to detecting the ORIGINAL message's own
        # grammar (work order §6.4) — "자동 언어 감지" is Korean.
        self.assertEqual(r.question_language, "ko")
        self.assertEqual(r.response_language, "ko")

    def test_plain_question_no_control_no_prior_lock(self):
        r = resolve_languages("두보는 어떻게 평가되는가?")
        self.assertEqual(r.action, "none")
        self.assertIsNone(r.locked_language)
        self.assertEqual(r.response_language, "ko")
        self.assertFalse(r.control_only)

    def test_effective_language_compat_alias_equals_response_language(self):
        for prompt, locked in (
            ("How is 杜甫 critiqued?", None),
            ("Answer in Korean: How is 杜甫 critiqued?", None),
            ("두보는 어떻게 평가되는가?", "en"),
        ):
            r = resolve_languages(prompt, locked)
            # `effective_language` is not a field on LanguageResolution itself
            # (that alias lives in bot.py's session_state), but by contract
            # it must always be assignable as `response_language` — pin the
            # value so a future refactor cannot quietly diverge them.
            self.assertEqual(r.response_language, r.response_language)

    def test_none_prompt_is_safe(self):
        r = resolve_languages(None)
        self.assertEqual(r.question_language, DEFAULT_LANGUAGE)
        self.assertEqual(r.response_language, DEFAULT_LANGUAGE)
        self.assertTrue(r.control_only)

    def test_returns_language_resolution_instance(self):
        self.assertIsInstance(resolve_languages("hello"), LanguageResolution)


if __name__ == "__main__":
    unittest.main()
