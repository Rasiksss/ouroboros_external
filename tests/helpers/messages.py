"""Message builder helpers extracted from test_compaction."""


def make_large_arg_messages(tool_name: str, num_rounds: int = 8) -> list[dict]:
    """Build messages whose old assistant tool-call payloads should compact."""
    messages = [{"role": "system", "content": [{"type": "text", "text": "system"}]}]
    large_args = '{"content": "' + ("x" * 1000) + '"}'
    for i in range(num_rounds):
        tc_id = f"call_{i}"
        messages.append(
            {
                "role": "assistant",
                "content": f"Round {i}",
                "tool_calls": [
                    {
                        "id": tc_id,
                        "function": {"name": tool_name, "arguments": large_args},
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tc_id,
                "content": "ok",
            }
        )
    return messages
