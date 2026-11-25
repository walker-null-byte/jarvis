"""Turn stored session messages into display items for the browser."""

import json
import re

SUMMARY_PREFIX = "[Summary of the earlier conversation]"
SUMMARY_ACK = "Understood — context noted."
ARTIFACT_RE = re.compile(r'"artifact_id":\s*"(artifact_[0-9a-f]+)"')
RESULT_PREVIEW = 2000


def _parse_args(raw):
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return {"_raw": str(raw)[:500]}
    return value if isinstance(value, dict) else {"_value": value}


def _tool_item(call, index):
    """Display item for one assistant tool call; unwraps the call_tool proxy."""
    fn = call.get("function") or {}
    name = fn.get("name", "")
    args = _parse_args(fn.get("arguments"))
    via = ""
    if name == "call_tool":
        via = "call_tool"
        name = str(args.get("name", "call_tool"))
        inner = args.get("arguments", {})
        args = _parse_args(inner)
    return {"type": "tool", "id": call.get("id", ""), "name": name, "args": args,
            "via": via, "result": None, "status": "pending", "index": index}


def render_history(messages, artifact_lookup=None):
    """Return a list of display items: user, assistant, tool, artifact, summary.

    user/assistant/tool items carry `index`, their position in `messages`
    (a tool item uses the index of the assistant message that made the call).
    """
    items = []
    pending = {}
    after_summary = False
    for index, message in enumerate(messages):
        role = message.get("role")
        content = message.get("content") or ""
        if role == "user":
            if content.startswith(SUMMARY_PREFIX):
                items.append({"type": "summary", "content": content[len(SUMMARY_PREFIX):].strip()})
                after_summary = True
            else:
                items.append({"type": "user", "content": content, "index": index})
                after_summary = False
        elif role == "assistant":
            if after_summary and content == SUMMARY_ACK:
                after_summary = False
                continue
            after_summary = False
            if content.strip():
                items.append({"type": "assistant", "content": content, "index": index})
            for call in message.get("tool_calls") or []:
                item = _tool_item(call, index)
                pending[item["id"]] = item
                items.append(item)
        elif role == "tool":
            item = pending.get(message.get("tool_call_id"))
            if item is None:
                continue
            item["result"] = content[:RESULT_PREVIEW]
            failed = content.startswith(("Error", "Tool arguments were not valid", "Cancelled"))
            item["status"] = "error" if failed else "done"
            match = ARTIFACT_RE.search(content)
            artifact = artifact_lookup(match.group(1)) if (match and artifact_lookup) else None
            if artifact:
                items.append({"type": "artifact", "artifact": artifact})
    return items
