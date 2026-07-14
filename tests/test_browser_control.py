"""Browser task policy tests: evidence freshness, authority, and recovery."""

from ares.browser_control import BrowserTaskController


SNAPSHOT = "- button 'Continue' [ref=e12]"
SNAPSHOT_TOOL = "mcp__playwright__browser_snapshot"
CLICK_TOOL = "mcp__playwright__browser_click"


def test_browser_turn_adds_goal_and_runtime_policy():
    controller = BrowserTaskController()

    guidance = controller.begin_turn("chat-a", "Open https://example.com and inspect the form")

    assert "Live Browser Task" in guidance
    assert "Authority: observe" in guidance
    assert "batch related fields" in guidance


def test_interaction_requires_fresh_snapshot_but_navigation_does_not():
    controller = BrowserTaskController()
    controller.begin_turn("chat-a", "Open the settings page")

    navigate = controller.before_call(
        "chat-a", "mcp__playwright__browser_navigate", {"url": "https://example.com"}
    )
    click = controller.before_call("chat-a", CLICK_TOOL, {"ref": "e12", "element": "Continue"})

    assert navigate.allowed
    assert not click.allowed
    assert "fresh accessibility snapshot" in click.message


def test_snapshot_is_cached_briefly_and_invalidated_after_mutation():
    controller = BrowserTaskController(snapshot_ttl_seconds=30)
    controller.begin_turn("chat-a", "Use the browser to complete the form")
    controller.after_call("chat-a", SNAPSHOT_TOOL, {}, SNAPSHOT)

    cached = controller.before_call("chat-a", SNAPSHOT_TOOL, {})
    click = controller.before_call("chat-a", CLICK_TOOL, {"ref": "e12", "element": "Continue"})
    result = controller.after_call(
        "chat-a", CLICK_TOOL, {"ref": "e12", "element": "Continue"}, "Clicked"
    )
    after_click = controller.before_call(
        "chat-a", CLICK_TOOL, {"ref": "e13", "element": "Next"}
    )

    assert cached.cached_result == SNAPSHOT
    assert click.allowed
    assert "verify the requested outcome" in result
    assert not after_click.allowed


def test_other_chat_mutation_invalidates_this_chats_snapshot():
    controller = BrowserTaskController()
    controller.begin_turn("chat-a", "Inspect the website")
    controller.after_call("chat-a", SNAPSHOT_TOOL, {}, SNAPSHOT)
    controller.begin_turn("chat-b", "Open another browser page")
    controller.after_call(
        "chat-b", "mcp__playwright__browser_navigate", {"url": "https://example.org"}, "Navigated"
    )

    preflight = controller.before_call(
        "chat-a", CLICK_TOOL, {"ref": "e12", "element": "Continue"}
    )

    assert not preflight.allowed


def test_consequential_action_needs_current_turn_authority():
    controller = BrowserTaskController()
    controller.begin_turn("chat-a", "Inspect the draft")
    controller.after_call("chat-a", SNAPSHOT_TOOL, {}, "- button 'Publish' [ref=e8]")

    blocked = controller.before_call(
        "chat-a", CLICK_TOOL, {"ref": "e8", "element": "Publish post"}
    )
    controller.begin_turn("chat-a", "Publish the post now")
    controller.after_call("chat-a", SNAPSHOT_TOOL, {}, "- button 'Publish' [ref=e8]")
    allowed = controller.before_call(
        "chat-a", CLICK_TOOL, {"ref": "e8", "element": "Publish post"}
    )

    assert not blocked.allowed
    assert "did not authorize" in blocked.message
    assert allowed.allowed


def test_negated_consequential_request_never_grants_authority():
    controller = BrowserTaskController()
    guidance = controller.begin_turn("chat-a", "Inspect the form but do not submit it")
    controller.after_call("chat-a", SNAPSHOT_TOOL, {}, "- button 'Submit' [ref=e9]")

    blocked = controller.before_call(
        "chat-a", CLICK_TOOL, {"ref": "e9", "element": "Submit"}
    )

    assert "Authority: observe" in guidance
    assert not blocked.allowed


def test_tab_selection_and_named_wait_can_start_without_page_refs():
    controller = BrowserTaskController()
    controller.begin_turn("chat-a", "Use the browser")

    tab = controller.before_call(
        "chat-a", "mcp__playwright__browser_tabs", {"action": "select", "index": 1}
    )
    wait = controller.before_call(
        "chat-a", "mcp__playwright__browser_wait_for", {"text": "Dashboard"}
    )

    assert tab.allowed
    assert wait.allowed


def test_stale_reference_gets_only_one_automatic_recovery():
    controller = BrowserTaskController()
    arguments = {"ref": "e12", "element": "Continue"}
    error = "Error: reference e12 is stale and not found"

    assert controller.should_recover_stale_ref("chat-a", CLICK_TOOL, arguments, error)
    assert not controller.should_recover_stale_ref("chat-a", CLICK_TOOL, arguments, error)
