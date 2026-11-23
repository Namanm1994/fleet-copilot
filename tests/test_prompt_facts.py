"""What the model is told before it plans, and the refusal that code writes.

Seen live (G-TC20): the planner wrote that the telemetry window "includes April 2026". Nothing had told it
which dates the data covers. Seen live (G-TC06): asked to wipe a device, the planner called it in scope, the
agent looked the device up twice and then refused in its own words.
"""

from __future__ import annotations

from tests.test_agent import plan, session, submit


def test_both_prompts_state_the_window_and_what_is_not_collected(home):
    sess, _ = session(home, [])
    lo, hi = (d[:10] for d in sess.tk.repo.data_window())
    for prompt in (sess.planner_prompt(), sess.agent_prompt({"steps": []})):
        assert f"covers {lo} to {hi}" in prompt
        assert "Network addresses (IP, MAC) and hardware UUIDs are not collected" in prompt
        assert "{window" not in prompt and "{tenant" not in prompt and "{forbidden" not in prompt   # every placeholder filled


def test_planner_prompt_lists_the_forbidden_actions_from_policy(home):
    sess, _ = session(home, [])
    forbidden = sess.tk.config.policy.forbidden_actions
    assert forbidden and all(name in sess.planner_prompt() for name in forbidden)


def test_forbidden_action_is_refused_in_code_without_reading_data(home):
    sess, model = session(home, [plan(intent_class="action", forbidden_action="wipe_device")])
    res = sess.ask("Wipe FX-DELL-01")
    assert res.status == "refused" and not res.proposals
    assert "I can't do that" in res.text and "wipe device" in res.text and "no approval can enable it" in res.text
    assert not model.steps and len(model.seen) == 1                       # one model call: the plan
    assert sess.tk.tool_corpus == []                                      # no tool ran
    refused = [a for a in sess.tk.audit.entries(sess.tk.ctx.tenant_id) if a.get("event") == "refused"]
    assert refused and "wipe_device" in str(refused[-1])


def test_a_name_that_policy_does_not_forbid_is_not_a_refusal(home):
    sess, _ = session(home, [plan(forbidden_action="open_remediation_ticket"),
                             submit({"summary": "Nothing to report.", "claims": [], "answer_type": "insufficient_data"})])
    assert sess.ask("Open a ticket").status == "answered"
