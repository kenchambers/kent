"""Unit tests for agent.memory.wings helpers."""
import pytest


def test_sanitize_wing_lowercases(tmp_path):
    from agent.memory.wings import sanitize_wing
    assert sanitize_wing("MyProject") == "myproject"


def test_sanitize_wing_valid_alnum(tmp_path):
    from agent.memory.wings import sanitize_wing
    assert sanitize_wing("proj1") == "proj1"


def test_sanitize_wing_valid_with_dash(tmp_path):
    from agent.memory.wings import sanitize_wing
    assert sanitize_wing("prod-deploys") == "prod-deploys"


def test_sanitize_wing_valid_with_underscore(tmp_path):
    from agent.memory.wings import sanitize_wing
    assert sanitize_wing("my_project") == "my_project"


def test_sanitize_wing_single_char(tmp_path):
    from agent.memory.wings import sanitize_wing
    assert sanitize_wing("a") == "a"


def test_sanitize_wing_max_length(tmp_path):
    from agent.memory.wings import sanitize_wing
    name = "a" * 64
    assert sanitize_wing(name) == name


def test_sanitize_wing_too_long_raises(tmp_path):
    from agent.memory.wings import sanitize_wing
    with pytest.raises(ValueError):
        sanitize_wing("a" * 65)


def test_sanitize_wing_starts_with_dash_raises():
    from agent.memory.wings import sanitize_wing
    with pytest.raises(ValueError):
        sanitize_wing("-badname")


def test_sanitize_wing_starts_with_underscore_raises():
    from agent.memory.wings import sanitize_wing
    with pytest.raises(ValueError):
        sanitize_wing("_badname")


def test_sanitize_wing_empty_raises():
    from agent.memory.wings import sanitize_wing
    with pytest.raises(ValueError):
        sanitize_wing("")


def test_sanitize_wing_unicode_raises():
    from agent.memory.wings import sanitize_wing
    with pytest.raises(ValueError):
        sanitize_wing("café")


def test_sanitize_wing_space_raises():
    from agent.memory.wings import sanitize_wing
    with pytest.raises(ValueError):
        sanitize_wing("my project")


def test_read_write_active_wing_roundtrip(tmp_path):
    from agent.memory.wings import read_active_wing, write_active_wing
    write_active_wing("testproject", home=tmp_path)
    assert read_active_wing(home=tmp_path) == "testproject"


def test_read_active_wing_default_when_missing(tmp_path):
    from agent.memory.wings import read_active_wing
    assert read_active_wing(default="fallback", home=tmp_path) == "fallback"


def test_read_active_wing_default_kent_default(tmp_path):
    from agent.memory.wings import read_active_wing
    assert read_active_wing(home=tmp_path) == "kent_default"


def test_write_active_wing_creates_parent_dirs(tmp_path):
    from agent.memory.wings import write_active_wing
    home = tmp_path / "deep" / "nested"
    write_active_wing("myproject", home=home)
    assert (home / "active_wing.txt").read_text() == "myproject"


def test_intent_roundtrip(tmp_path):
    from agent.memory.wings import read_intent, write_intent
    write_intent("proj", "monitors terraform deploys", home=tmp_path)
    assert read_intent("proj", home=tmp_path) == "monitors terraform deploys"


def test_read_intent_returns_none_when_missing(tmp_path):
    from agent.memory.wings import read_intent
    assert read_intent("nonexistent", home=tmp_path) is None


def test_list_wings_empty_when_no_diaries(tmp_path):
    from agent.memory.wings import list_wings
    assert list_wings(home=tmp_path) == []


def test_list_wings_returns_valid_dirs(tmp_path):
    from agent.memory.wings import list_wings
    diaries = tmp_path / "diaries"
    (diaries / "alpha").mkdir(parents=True)
    (diaries / "beta").mkdir(parents=True)
    result = list_wings(home=tmp_path)
    assert result == ["alpha", "beta"]


def test_list_wings_skips_dot_dirs(tmp_path):
    from agent.memory.wings import list_wings
    diaries = tmp_path / "diaries"
    (diaries / ".hidden").mkdir(parents=True)
    (diaries / "visible").mkdir(parents=True)
    result = list_wings(home=tmp_path)
    assert result == ["visible"]


def test_list_wings_skips_invalid_names(tmp_path):
    from agent.memory.wings import list_wings
    diaries = tmp_path / "diaries"
    (diaries / "valid-wing").mkdir(parents=True)
    (diaries / "INVALID_UPPER").mkdir(parents=True)  # uppercase — not matching regex
    result = list_wings(home=tmp_path)
    assert result == ["valid-wing"]


def test_list_wings_skips_files_not_dirs(tmp_path):
    from agent.memory.wings import list_wings
    diaries = tmp_path / "diaries"
    diaries.mkdir(parents=True)
    (diaries / "not-a-dir.txt").write_text("nope")
    (diaries / "real-wing").mkdir()
    result = list_wings(home=tmp_path)
    assert result == ["real-wing"]
