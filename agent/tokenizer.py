def approx_tokens(text: str) -> int:
    return len(text) // 4


def count_messages_tokens(messages: list) -> int:
    total = 0
    for m in messages:
        total += approx_tokens(m.get("content", "") or "")
        if "tool_calls" in m:
            for tc in m["tool_calls"]:
                total += approx_tokens(tc["function"]["name"])
                total += approx_tokens(tc["function"]["arguments"])
    return total
