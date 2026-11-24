"""The adjudication validator must not treat one file as two raters.

On 2026-11-16 the committed verdict files were byte-identical, so the recorded
100% inter-rater agreement was a file compared with itself. These tests pin the
guard that makes that state fail instead of passing.
"""

from pathlib import Path

from scripts.validate_adjudication import verdict_files_identical


def test_identical_verdict_files_are_detected(tmp_path: Path) -> None:
    first = tmp_path / "verdicts_1.json"
    second = tmp_path / "verdicts_2.json"
    payload = '{"0xabc": "could_be_self_dealing"}'
    first.write_text(payload, encoding="utf-8")
    second.write_text(payload, encoding="utf-8")
    assert verdict_files_identical(first, second) is True


def test_distinct_verdict_files_are_not_flagged(tmp_path: Path) -> None:
    first = tmp_path / "verdicts_1.json"
    second = tmp_path / "verdicts_2.json"
    first.write_text('{"0xabc": "could_be_self_dealing"}', encoding="utf-8")
    second.write_text('{"0xabc": "cannot_tell"}', encoding="utf-8")
    assert verdict_files_identical(first, second) is False
