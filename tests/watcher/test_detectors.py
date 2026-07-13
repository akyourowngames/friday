import pytest

from ares.watcher.detectors import DiffDetector, HashDetector, ThresholdDetector, canonicalize


def test_hash_detector_change_and_stability():
    detector = HashDetector()
    assert detector.detect("hello", "hello").changed is False
    result = detector.detect("hello", "world")
    assert result.changed is True
    assert result.old_hash != result.new_hash


def test_hash_detector_can_ignore_dynamic_noise():
    result = HashDetector().detect("price 20 generated=123", "price 20 generated=999", ignore_patterns=[r"generated=\d+"])
    assert result.changed is False


def test_diff_detector_reports_added_removed_and_similarity():
    result = DiffDetector().detect("one\ntwo", "one\nthree")
    assert result.changed is True
    assert result.details["added"] == 1
    assert result.details["removed"] == 1
    assert "+three" in result.details["diff"]


@pytest.mark.parametrize("old,new,config,changed", [
    (100,89,{"max_change_pct":10},True), (100,95,{"max_change_pct":10},False),
    (110,99,{"alert_below":100},True), (90,101,{"alert_above":100},True),
    (10,13,{"max_change_abs":3},True),
])
def test_threshold_detection(old,new,config,changed):
    assert ThresholdDetector().detect(old,new,config).changed is changed


def test_threshold_handles_zero_and_missing_values():
    assert ThresholdDetector().detect(0,5,{}).changed is True
    assert ThresholdDetector().detect(None,5,{}).changed is False


def test_canonicalize_json_and_invalid_pattern():
    assert canonicalize({"b":2,"a":1}) == '{"a":1,"b":2}'
    with pytest.raises(ValueError): canonicalize("x", ["["])
