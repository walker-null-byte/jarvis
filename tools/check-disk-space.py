"""
Auto-generated tool from skill: check-disk-space
Check Disk Space

Steps:
  1. Run the `df -h /` command to get disk usage for the root filesystem
  2. Extract the available space and usage percentage from the output
  3. Report the result to the user in a clear format

Tools needed: run_command
"""

import json
from contextaware import ContextAware


def check_disk_space(**kwargs):
    """Execute the check-disk-space skill procedure."""
    # Import here to avoid circular imports
    from jarvis.tools_core import build_registry
    from jarvis.mind_tools import build_mind_tools
    from jarvis.config import load_config, mind_dir
    from jarvis.context import ensure_mind
    from jarvis.llm import LLM

    cfg = load_config()
    mind = ensure_mind(cfg)
    registry = build_registry()
    for f in build_mind_tools(mind):
        registry.register(f)

    # Get available tools for the LLM to choose from
    available_tools = registry.to_openai_tools()
    llm = LLM(cfg)

    results = []
    context = {}  # Accumulated context from previous steps

    for i, step in enumerate(["Run the `df -h /` command to get disk usage for the root filesystem", "Extract the available space and usage percentage from the output", "Report the result to the user in a clear format"]):
        step_num = i + 1
        try:
            # Build a prompt for the LLM to translate this step into tool calls
            context_summary = ""
            if context:
                context_summary = "\nPrevious results:\n" + json.dumps(context, indent=2)[:3000]

            prompt = f"""You are executing step {step_num} of the "check-disk-space" skill.
Skill: Check Disk Space
Step: {step}
Available tools: {', '.join(t['function']['name'] for t in available_tools)}
{context_summary}

Respond with ONLY a JSON object:
{
  "tool": "tool_name",
  "arguments": {"arg": "value"}
}
Or if no tool is needed (e.g., pure computation), respond with:
{
  "tool": null,
  "result": "computed result"
}"""

            messages = [
                {"role": "system", "content": "You are a tool executor. Output ONLY valid JSON."},
                {"role": "user", "content": prompt}
            ]
            response = llm.chat(messages, tools=available_tools)
            content = (response.get("content") or "").strip()

            # Parse LLM response
            try:
                decision = json.loads(content)
            except json.JSONDecodeError:
                # Try to extract JSON from markdown/code fences
                import re
                match = re.search(r'\{.*\}', content, re.DOTALL)
                if match:
                    decision = json.loads(match.group(0))
                else:
                    raise

            if decision.get("tool"):
                tool_name = decision["tool"]
                arguments = decision.get("arguments", {})
                # Execute the tool
                tool_result = registry.call(tool_name, arguments)
                results.append({
                    "step": step_num,
                    "description": step,
                    "tool_called": tool_name,
                    "arguments": arguments,
                    "result": tool_result,
                    "status": "executed"
                })
                context[f"step_{step_num}_result"] = tool_result
            else:
                # No tool needed - just record the computed result
                result = decision.get("result", "")
                results.append({
                    "step": step_num,
                    "description": step,
                    "tool_called": None,
                    "result": result,
                    "status": "computed"
                })
                context[f"step_{step_num}_result"] = result

        except Exception as e:
            results.append({"step": step_num, "description": step, "error": str(e), "status": "failed"})
            break

    return {
        "skill": "check-disk-space",
        "executed": True,
        "steps": results,
        "context": context,
    }


def register(registry):
    """Register this tool with a ContextAware registry."""
    registry.register(check_disk_space, name="check-disk-space", description="Check Disk Space")
    return check_disk_space
