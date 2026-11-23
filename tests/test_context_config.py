"""Context limits are settings in policy.yaml (context:), not constants in code."""

from __future__ import annotations

import pytest

from fleet_copilot.config import ConfigError, load_app_config
from tests.test_agent import session


# ---- context limits come from policy.yaml -----------------------------------------------------------
def test_context_limits_are_read_from_policy(home):
    p = home.config / "policy.yaml"
    p.write_text(p.read_text(encoding="utf-8").replace("history_turns: 3", "history_turns: 1")
                 .replace("focus_queries: 4", "focus_queries: 1").replace("max_chars_per_turn: 48000", "max_chars_per_turn: 9000"),
                 encoding="utf-8")
    sess, _ = session(home, [])
    assert (sess.history_turns, sess.focus_queries, sess.context_max_chars) == (1, 1, 9000)
    assert sess.max_tool_output_chars == 12000 and sess.digest_max_chars == 1500
    for n in range(1, 4):
        sess.history.append({"turn": n, "question": f"q{n}", "summary": f"a{n}", "focus": [], "proposals": []})
    assert len(sess.history_messages()) == 2 and "turn 2:" in sess.history_text()


def test_context_defaults_apply_when_the_section_is_missing(home):
    p = home.config / "policy.yaml"
    text = p.read_text(encoding="utf-8")
    start, end = text.index("# Context management"), text.index("upgrade_specs:")
    p.write_text(text[:start] + text[end:], encoding="utf-8")
    assert load_app_config(home.config, "policy.yaml").policy.context["history_turns"] == 3


@pytest.mark.parametrize("old,new", [("history_turns: 3", "history_turns: 0"), ("digest_max_chars: 1500", "digest_max_chars: lots"),
                                     ("focus_queries: 4", "focus_queries: 4\n  made_up_setting: 5")])
def test_bad_context_settings_stop_startup(home, old, new):
    p = home.config / "policy.yaml"
    p.write_text(p.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_app_config(home.config, "policy.yaml")
