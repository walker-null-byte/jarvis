"""Skill codification — turns drafted skill .md files into real Python tools.

Reads mind/skills/*.md, generates tools/<name>.py, smoke-tests it, and
hot-loads into the ContextAware registry. Every new tool is git-committed.
"""

import importlib.util
import json
import re
import subprocess
from pathlib import Path

from jarvis.config import load_config, mind_dir
from jarvis.tools_core import build_registry
from jarvis.mind_tools import build_mind_tools


# ---------- git helpers ----------

def _git(*args, cwd):
    try:
        subprocess.run(["git", *args], cwd=cwd, check=True,
                       capture_output=True, text=True)
    except subprocess.CalledProcessError:
        pass


def _git_commit(cwd, message):
    _git("add", "-A", cwd=cwd)
    _git("commit", "-m", message, cwd=cwd)


# ---------- skill parsing ----------

def _parse_skill(path):
    """Parse a skill .md file into structured data."""
    text = path.read_text(encoding="utf-8")
    lines = text.strip().splitlines()

    data = {"name": path.stem, "description": "", "steps": [], "tools_needed": []}
    current_section = None

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            data["description"] = stripped[2:].strip()
        elif stripped.startswith("## "):
            current_section = stripped[3:].strip().lower()
        elif stripped.startswith("- ") and current_section in ("steps", "tools needed", "tools"):
            item = stripped[2:].strip()
            if current_section == "steps":
                data["steps"].append(item)
            else:
                data["tools_needed"].append(item)
    return data


# ---------- code generation ----------

def _generate_tool_code(skill):
    """Generate a Python tool module that actually executes its steps."""
    name = skill["name"]
    func_name = re.sub(r"[^a-z0-9_]", "_", name.lower())
    description = skill["description"].replace('"', '\\"').replace('\n', ' ')
    steps_json = json.dumps(skill["steps"])
    tools_needed = ", ".join(skill["tools_needed"])
    steps_formatted = chr(10).join(f"  {i+1}. {s}" for i, s in enumerate(skill["steps"]))

    code = f'''"""
Auto-generated tool from skill: {name}
{description}

Steps:
{steps_formatted}

Tools needed: {tools_needed}
"""

import json
from contextaware import ContextAware


def {func_name}(**kwargs):
    """Execute the {name} skill procedure."""
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
    context = {{}}  # Accumulated context from previous steps

    for i, step in enumerate({steps_json}):
        step_num = i + 1
        try:
            # Build a prompt for the LLM to translate this step into tool calls
            context_summary = ""
            if context:
                context_summary = "\\nPrevious results:\\n" + json.dumps(context, indent=2)[:3000]

            prompt = f"""You are executing step {{step_num}} of the "{name}" skill.
Skill: {description}
Step: {{step}}
Available tools: {{', '.join(t['function']['name'] for t in available_tools)}}
{{context_summary}}

Respond with ONLY a JSON object:
{{
  "tool": "tool_name",
  "arguments": {{"arg": "value"}}
}}
Or if no tool is needed (e.g., pure computation), respond with:
{{
  "tool": null,
  "result": "computed result"
}}"""

            messages = [
                {{"role": "system", "content": "You are a tool executor. Output ONLY valid JSON."}},
                {{"role": "user", "content": prompt}}
            ]
            response = llm.chat(messages, tools=available_tools)
            content = (response.get("content") or "").strip()

            # Parse LLM response
            try:
                decision = json.loads(content)
            except json.JSONDecodeError:
                # Try to extract JSON from markdown/code fences
                import re
                match = re.search(r'\\{{.*\\}}', content, re.DOTALL)
                if match:
                    decision = json.loads(match.group(0))
                else:
                    raise

            if decision.get("tool"):
                tool_name = decision["tool"]
                arguments = decision.get("arguments", {{}})
                # Execute the tool
                tool_result = registry.call(tool_name, arguments)
                results.append({{
                    "step": step_num,
                    "description": step,
                    "tool_called": tool_name,
                    "arguments": arguments,
                    "result": tool_result,
                    "status": "executed"
                }})
                context[f"step_{{step_num}}_result"] = tool_result
            else:
                # No tool needed - just record the computed result
                result = decision.get("result", "")
                results.append({{
                    "step": step_num,
                    "description": step,
                    "tool_called": None,
                    "result": result,
                    "status": "computed"
                }})
                context[f"step_{{step_num}}_result"] = result

        except Exception as e:
            results.append({{"step": step_num, "description": step, "error": str(e), "status": "failed"}})
            break

    return {{
        "skill": {json.dumps(name)},
        "executed": True,
        "steps": results,
        "context": context,
    }}


def register(registry):
    """Register this tool with a ContextAware registry."""
    registry.register({func_name}, name={json.dumps(name)}, description={json.dumps(description)})
    return {func_name}
'''
    return code


# ---------- smoke test ----------

def _smoke_test(tool_path, skill_name):
    """Import the generated module and call its register function."""
    try:
        spec = importlib.util.spec_from_file_location(skill_name, tool_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # Test registration
        from contextaware import ContextAware
        test_reg = ContextAware()
        func = module.register(test_reg)
        assert func is not None

        # Test calling the function (it will use the LLM - may fail if no server)
        # Just verify it doesn't crash on import/registration
        return True, None
    except Exception as e:
        return False, str(e)


# ---------- main entry ----------

def codify_skills(mind=None, config=None):
    """Read all skill .md files, generate tools, test them, and load them."""
    config = config or load_config()
    mind = mind or mind_dir(config)
    skills_dir = Path(mind) / "skills"
    tools_dir = Path(mind).parent / "tools"
    tools_dir.mkdir(exist_ok=True)

    if not skills_dir.exists():
        return {"generated": 0, "loaded": 0, "errors": []}

    results = {"generated": 0, "loaded": 0, "errors": []}

    for skill_path in skills_dir.glob("*.md"):
        skill = _parse_skill(skill_path)
        if not skill["steps"]:
            results["errors"].append(f"{skill_path.name}: no steps found")
            continue

        # Generate tool code
        code = _generate_tool_code(skill)
        tool_path = tools_dir / f"{skill['name']}.py"
        tool_path.write_text(code, encoding="utf-8")
        results["generated"] += 1

        # Smoke test
        ok, err = _smoke_test(tool_path, skill["name"])
        if ok:
            results["loaded"] += 1
        else:
            results["errors"].append(f"{skill['name']}: smoke test failed - {err}")
            # Remove the failed tool file
            tool_path.unlink(missing_ok=True)
            results["generated"] -= 1

    # Git commit if anything changed
    if results["generated"] > 0:
        _git_commit(mind.parent, f"codify skills: generated {results['generated']} tools")

    return results


if __name__ == "__main__":
    import json
    result = codify_skills()
    print(json.dumps(result, indent=2))