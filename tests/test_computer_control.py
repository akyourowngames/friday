from __future__ import annotations

from ares.integrations.computer_control import (
    ComputerTaskController,
    GenericWindowsAdapter,
    SendMessagePhase,
    TelegramDesktopAdapter,
)


def telegram_snapshot(
    *,
    selected: str = "Sujal Mankar",
    draft: str = "",
    focused: str = "message_composer",
    search_text: str = "",
    overlay: bool = False,
    outgoing: str = "",
    search_results: tuple[str, ...] = (),
) -> str:
    search_meta = "  [focused]" if focused == "global_search" else ""
    if search_text:
        search_meta += f'  [value:"{search_text}"]'
    composer_meta = "  [focused]" if focused == "message_composer" else ""
    if draft:
        composer_meta += f'  [value:"{draft}"]'
    header = (
        f'        │   └── (800,45) text "{selected}"  [action: click]\n'
        if selected
        else ""
    )
    overlay_tree = ""
    if overlay or search_results:
        rows = "\n".join(
            f'        │   ├── (250,{120 + index * 45}) list item "{name}"  [action: click]'
            for index, name in enumerate(search_results)
        )
        overlay_tree = (
            '    ├── pane "SearchOverlay"\n'
            '    │   └── group "SearchResults"\n'
            f"{rows}\n"
        )
    outgoing_tree = (
        f'        │   └── (900,600) text "{outgoing}"  [action: click]\n'
        if outgoing
        else '        │   └── (900,600) text "Earlier message"  [action: click]\n'
    )
    return f"""Cursor Position: (900,745)

Focused Window:
Name       Depth  Status       Width    Height    Handle
---------  -------  ---------  -------  --------  --------
Telegram         0  Maximized     1366       768      1001

Opened Windows:
Name       Depth  Status       Width    Height    Handle
---------  -------  ---------  -------  --------  --------
Telegram         0  Maximized     1366       768      1001

UI Tree:
desktop
└── window "Telegram"
    ├── pane "LeftNavigationPane"
    │   └── (250,50) edit "Search"  [action: fill]{search_meta}
{overlay_tree}    └── pane "ActiveChatPane"
        ├── group "ChatHeader"
{header}        ├── pane "MessageHistory"
{outgoing_tree}        └── group "MessageComposer"
            ├── (900,745) edit "Message"  [action: fill]{composer_meta}
            └── (1320,745) button "Send"  [action: click]
"""


def generic_snapshot() -> str:
    return """Focused Window:
Name        Depth  Status  Width  Height  Handle
----------  -----  ------  -----  ------  ------
ComplexApp      0  Normal   1000     700      42

Opened Windows:

UI Tree:
desktop
└── window "ComplexApp"
    ├── pane "Sidebar"
    │   └── (180,80) edit "Filter"  [action: fill]
    └── pane "Document"
        └── (650,620) edit "Text"  [action: fill]  [focused]
"""


def begin_message_task(controller: ComputerTaskController, session: str = "chat"):
    guidance = controller.begin_turn(
        session,
        'Send a Telegram message to Sujal Mankar saying "Call me at 6"',
        routing_text="desktop",
    )
    assert "Immutable target" in guidance


def meta(
    controller: ComputerTaskController,
    *,
    session: str = "chat",
    region: str,
    intent: str,
    phase: str | None = None,
    owner: str = "",
    entity: str = "Sujal Mankar",
):
    state = controller.state_for(session)
    value = {
        "expected_app": state.app_state.app_name,
        "expected_region": region,
        "purpose": intent.replace("_", " "),
        "semantic_intent": intent,
        "phase": phase or state.phase.value,
        "entity": entity,
        "ui_generation": state.ui_generation,
    }
    if owner:
        value["text_owner"] = owner
    return value


def test_telegram_adapter_recognizes_semantic_paths_not_flat_edit_labels():
    state = TelegramDesktopAdapter().parse_state(
        telegram_snapshot(draft="Sujal Mankar")
    )

    assert state.selected_entity == "Sujal Mankar"
    assert state.focused_region == "message_composer"
    assert state.region("global_search").element.semantic_path.endswith(
        "LeftNavigationPane / Search"
    )
    assert state.region("message_composer").element.semantic_path.endswith(
        "ActiveChatPane / MessageComposer / Message"
    )


def test_target_chat_already_open_skips_search_and_detects_bad_draft():
    controller = ComputerTaskController()
    begin_message_task(controller)

    projected = controller.after_snapshot(
        "chat", telegram_snapshot(draft="Sujal Mankar")
    )
    state = controller.state_for("chat")

    assert state.phase is SendMessagePhase.FOCUS_COMPOSER
    assert SendMessagePhase.SEARCH_CONTACT in state.completed_phases
    assert "incorrectly contains target_name" in state.recovery_reason
    assert '"ui_generation": 1' in projected


def test_target_name_can_never_be_typed_into_message_composer():
    controller = ComputerTaskController()
    begin_message_task(controller)
    controller.after_snapshot("chat", telegram_snapshot(draft="Sujal Mankar"))
    arguments = {
        "text": "Sujal Mankar",
        "loc": [900, 745],
        "clear": True,
        "__ares": meta(
            controller,
            region="message_composer",
            intent="type_message",
            owner="message_composer",
        ),
    }

    decision = controller.before_call("chat", "mcp__windows__Type", arguments)

    assert decision.allowed is False
    assert "Rejected target_name" in decision.message
    assert "Call me at 6" in decision.message


def test_message_text_can_never_be_typed_into_global_search():
    controller = ComputerTaskController()
    begin_message_task(controller)
    controller.after_snapshot(
        "chat",
        telegram_snapshot(selected="", focused="global_search", overlay=True),
    )
    arguments = {
        "text": "Call me at 6",
        "loc": [250, 50],
        "__ares": meta(
            controller,
            region="global_search",
            intent="search_contact",
            owner="global_search",
        ),
    }

    decision = controller.before_call("chat", "mcp__windows__Type", arguments)

    assert decision.allowed is False
    assert "owns only target_name" in decision.message


def test_type_rejects_correct_region_until_that_subtree_is_verified_focused():
    controller = ComputerTaskController()
    begin_message_task(controller)
    controller.after_snapshot(
        "chat",
        telegram_snapshot(selected="Sujal Mankar", focused=""),
    )
    arguments = {
        "text": "Call me at 6",
        "loc": [900, 745],
        "__ares": meta(
            controller,
            region="message_composer",
            intent="type_message",
            owner="message_composer",
        ),
    }

    decision = controller.before_call("chat", "mcp__windows__Type", arguments)

    assert decision.allowed is False
    assert "expected focused subtree 'message_composer'" in decision.message


def test_verified_chat_invalidates_all_old_search_actions():
    controller = ComputerTaskController()
    begin_message_task(controller)
    controller.after_snapshot("chat", telegram_snapshot())
    arguments = {
        "loc": [250, 50],
        "__ares": meta(
            controller,
            region="global_search",
            intent="search_contact",
        ),
    }

    decision = controller.before_call("chat", "mcp__windows__Click", arguments)

    assert decision.allowed is False
    assert "already verified" in decision.message


def test_fresh_snapshot_invalidates_previous_ui_generation():
    controller = ComputerTaskController()
    begin_message_task(controller)
    controller.after_snapshot("chat", telegram_snapshot())
    stale_generation = controller.state_for("chat").ui_generation
    controller.after_snapshot("chat", telegram_snapshot(focused=""))
    arguments = {
        "loc": [900, 745],
        "__ares": {
            **meta(
                controller,
                region="message_composer",
                intent="focus_composer",
            ),
            "ui_generation": stale_generation,
        },
    }

    decision = controller.before_call("chat", "mcp__windows__Click", arguments)

    assert decision.allowed is False
    assert "Stale UI reference" in decision.message


def test_wrong_chat_and_search_overlay_do_not_verify_target():
    controller = ComputerTaskController()
    begin_message_task(controller)

    controller.after_snapshot(
        "chat",
        telegram_snapshot(
            selected="Saved Messages",
            focused="global_search",
            search_text="Sujal Mankar",
            overlay=True,
            search_results=("Saved Messages", "Sujal Mankar"),
        ),
    )
    state = controller.state_for("chat")

    assert state.app_state.selected_entity == "Saved Messages"
    assert state.app_state.search_mode is True
    assert state.phase is SendMessagePhase.SELECT_CONTACT
    assert SendMessagePhase.VERIFY_CHAT not in state.completed_phases


def test_duplicate_result_labels_require_row_ancestry_and_coordinate():
    controller = ComputerTaskController()
    begin_message_task(controller)
    controller.after_snapshot(
        "chat",
        telegram_snapshot(
            selected="",
            focused="global_search",
            search_text="Sujal Mankar",
            overlay=True,
            search_results=("Sujal Mankar", "Sujal Mankar"),
        ),
    )
    arguments = {
        "label": "Sujal Mankar",
        "__ares": meta(
            controller,
            region="search_results",
            intent="select_contact",
        ),
    }

    ambiguous = controller.before_call(
        "chat", "mcp__windows__Click", arguments
    )
    arguments["loc"] = [250, 165]
    arguments.pop("label")
    precise = controller.before_call("chat", "mcp__windows__Click", arguments)

    assert ambiguous.allowed is False
    assert "ambiguous" in ambiguous.message
    assert precise.allowed is True


def test_successful_message_send_requires_empty_draft_and_outgoing_bubble():
    controller = ComputerTaskController()
    begin_message_task(controller)
    controller.after_snapshot(
        "chat", telegram_snapshot(draft="Call me at 6")
    )
    assert controller.state_for("chat").phase is SendMessagePhase.SEND
    arguments = {
        "loc": [1320, 745],
        "__ares": meta(
            controller,
            region="send_button",
            intent="send_message",
        ),
    }
    decision = controller.before_call("chat", "mcp__windows__Click", arguments)
    assert decision.allowed is True
    controller.after_action(
        "chat", "mcp__windows__Click", arguments, "Clicked successfully", decision.action
    )

    result = controller.after_snapshot(
        "chat", telegram_snapshot(draft="", outgoing="Call me at 6")
    )

    assert controller.state_for("chat").phase is SendMessagePhase.COMPLETE
    assert "passed: composer_empty=True, outgoing_message_visible=True" in result


def test_empty_draft_without_outgoing_bubble_remains_in_verify_sent():
    controller = ComputerTaskController()
    begin_message_task(controller)
    controller.after_snapshot(
        "chat", telegram_snapshot(draft="Call me at 6")
    )
    arguments = {
        "loc": [1320, 745],
        "__ares": meta(
            controller,
            region="send_button",
            intent="send_message",
        ),
    }
    decision = controller.before_call("chat", "mcp__windows__Click", arguments)
    controller.after_action(
        "chat", "mcp__windows__Click", arguments, "Clicked successfully", decision.action
    )

    result = controller.after_snapshot("chat", telegram_snapshot(draft=""))

    assert controller.state_for("chat").phase is SendMessagePhase.VERIFY_SENT
    assert "failed: composer_empty=True, outgoing_message_visible=False" in result


def test_pointer_move_does_not_require_a_new_snapshot_before_click():
    controller = ComputerTaskController()
    begin_message_task(controller)
    controller.after_snapshot(
        "chat", telegram_snapshot(selected="", focused="global_search", overlay=True)
    )

    move_result = controller.after_action(
        "chat",
        "mcp__windows__Move",
        {"loc": [250, 50]},
        "Moved pointer successfully",
    )
    click_arguments = {
        "loc": [250, 50],
        "__ares": meta(
            controller,
            region="global_search",
            intent="focus_search",
        ),
    }
    click = controller.before_call(
        "chat", "mcp__windows__Click", click_arguments
    )

    assert move_result == "Moved pointer successfully"
    assert controller.state_for("chat").observation_required is False
    assert click.allowed is True


def test_semantic_loop_stops_third_equivalent_action_on_unchanged_ui():
    controller = ComputerTaskController()
    begin_message_task(controller)
    snapshot = telegram_snapshot(selected="", focused="global_search", overlay=True)
    controller.after_snapshot("chat", snapshot)

    for _index in range(2):
        arguments = {
            "loc": [250, 50],
            "__ares": meta(
                controller,
                region="global_search",
                intent="focus_search",
            ),
        }
        decision = controller.before_call(
            "chat", "mcp__windows__Click", arguments
        )
        assert decision.allowed is True
        controller.after_action(
            "chat", "mcp__windows__Click", arguments, "Clicked successfully", decision.action
        )
        controller.after_snapshot("chat", snapshot)

    third_arguments = {
        "loc": [250, 50],
        "__ares": meta(
            controller,
            region="global_search",
            intent="focus_search",
        ),
    }
    third = controller.before_call(
        "chat", "mcp__windows__Click", third_arguments
    )

    assert third.allowed is False
    assert "Semantic loop detected" in third.message


def test_unknown_complex_app_still_requires_region_generation_and_exact_target():
    controller = ComputerTaskController(adapters=(GenericWindowsAdapter(),))
    controller.begin_turn(
        "generic", "Use the desktop app to update the document", routing_text="desktop"
    )
    controller.after_snapshot("generic", generic_snapshot())
    state = controller.state_for("generic")
    missing_metadata = controller.before_call(
        "generic",
        "mcp__windows__Type",
        {"text": "hello", "loc": [650, 620]},
    )
    wrong_region = controller.before_call(
        "generic",
        "mcp__windows__Type",
        {
            "text": "hello",
            "loc": [650, 620],
            "__ares": {
                "expected_app": state.app_state.app_name,
                "expected_region": "global_search",
                "purpose": "edit the document",
                "semantic_intent": "type_document",
                "phase": state.phase.value,
                "text_owner": "global_search",
                "ui_generation": state.ui_generation,
            },
        },
    )

    assert missing_metadata.allowed is False
    assert "ui_generation" in missing_metadata.message
    assert wrong_region.allowed is False
    assert "focused region is 'editor'" in wrong_region.message


def test_bootstrap_launch_is_guarded_without_requiring_a_nonexistent_generation():
    controller = ComputerTaskController()
    controller.begin_turn(
        "launch", "Open the Telegram desktop app", routing_text="desktop"
    )
    arguments = {
        "name": "Telegram",
        "__ares": {
            "expected_app": "telegram",
            "expected_region": "application",
            "purpose": "open the requested app",
            "semantic_intent": "launch_app",
            "phase": "open_app",
        },
    }

    decision = controller.before_call(
        "launch", "mcp__windows__Launch", arguments
    )

    assert decision.allowed is True


def test_structured_trace_contains_precondition_and_state_identity():
    controller = ComputerTaskController()
    begin_message_task(controller)
    controller.after_snapshot("chat", telegram_snapshot())
    arguments = {
        "loc": [250, 50],
        "__ares": meta(
            controller,
            region="global_search",
            intent="search_contact",
        ),
    }
    controller.before_call("chat", "mcp__windows__Click", arguments)

    trace = controller.traces[-1].as_dict()
    assert trace["task_id"]
    assert trace["app"] == "telegram"
    assert trace["phase"] == SendMessagePhase.TYPE_MESSAGE.value
    assert trace["ui_fingerprint"]
    assert trace["precondition_result"].startswith("rejected:")
