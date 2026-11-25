"""Skill codification — turns drafted skill .md files into real Python tools.

Reads mind/skills/*.md, generates tools/<name>.py, smoke-tests it, and
hot-loads into the turbomcp registry. Every new tool is git-committed.
"""

import importlib.util
import ast
import json
import re
import subprocess
from pathlib import Path

from jarvis.config import load_config, mind_dir


# ---------- git helpers ----------

def _git(*args, cwd):
    try:
        subprocess.run(["git", *args], cwd=cwd, check=True,
                       capture_output=True, text=True)
    except subprocess.CalledProcessError:
        pass


def _git_commit(cwd, message, paths=None):
    if paths:
        _git("add", "-A", "--", *[str(path) for path in paths], cwd=cwd)
    else:
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
        elif current_section in ("steps", "tools needed", "tools") and (
            stripped.startswith("- ") or re.match(r"^\d+[.)]\s+", stripped)
        ):
            item = re.sub(r"^(?:- |\d+[.)]\s+)", "", stripped).strip()
            if current_section == "steps":
                data["steps"].append(item)
            else:
                data["tools_needed"].append(item)
        elif current_section in ("tools needed", "tools") and stripped:
            data["tools_needed"].extend(item.strip() for item in stripped.split(",") if item.strip())
    return data


# ---------- code generation ----------

TOOL_TEMPLATE = '''__DOC__

import json
import re


def __FUNC__(**kwargs):
    """Execute the __NAME__ skill procedure."""
    # Imported here to avoid circular imports at load time.
    from jarvis.config import load_config
    from jarvis.llm import LLM
    from jarvis.registry import get_registry

    cfg = load_config()
    registry = get_registry(cfg)  # shared registry: approvals are wired by the running agent
    llm = LLM(cfg)

    # Offer the nested model only the tools this skill needs (fall back to core tools).
    existing = set(registry.tool_names())
    wanted = [w for item in __TOOLS__ for w in re.findall(r"[A-Za-z0-9_]+", item) if w in existing]
    names = sorted(set(wanted)) or [n for n in cfg["agent"]["core_tools"] if n in existing]
    tools = registry.to_openai_tools(names)

    results = []
    context = {}  # results of previous steps

    for step_num, step in enumerate(__STEPS__, start=1):
        try:
            prompt = "Skill: __DESC__\\nStep " + str(step_num) + ": " + step
            if context:
                prompt += "\\nPrevious results:\\n" + json.dumps(context, indent=2, default=str)[:3000]
            messages = [
                {"role": "system", "content": "You execute one step of a skill. Call the provided tools to do it; if no tool is needed, answer in plain text."},
                {"role": "user", "content": prompt},
            ]
            response = llm.chat(messages, tools=tools)

            calls = response.get("tool_calls") or []
            if not calls:
                # Fallback for models that answer with JSON text instead of tool calls.
                content = (response.get("content") or "").strip()
                match = re.search(r"\\{.*\\}", content, re.DOTALL)
                try:
                    decision = json.loads(match.group(0)) if match else {}
                except ValueError:
                    decision = {}
                if decision.get("tool"):
                    calls = [{"function": {"name": decision["tool"], "arguments": json.dumps(decision.get("arguments", {}))}}]
                else:
                    results.append({"step": step_num, "description": step, "result": decision.get("result", content), "status": "computed"})
                    context["step_" + str(step_num) + "_result"] = decision.get("result", content)
                    continue

            for call in calls:
                tool_name = call["function"]["name"]
                arguments = json.loads(call["function"]["arguments"] or "{}")
                if tool_name not in names:
                    raise ValueError("tool not allowed for this skill: " + tool_name)
                tool_result = registry.call(tool_name, arguments)
                results.append({"step": step_num, "description": step, "tool_called": tool_name,
                                "arguments": arguments, "result": tool_result, "status": "executed"})
                context["step_" + str(step_num) + "_result"] = tool_result
        except Exception as e:
            results.append({"step": step_num, "description": step, "error": str(e), "status": "failed"})
            break

    return {"skill": __NAME_JSON__, "executed": True, "steps": results, "context": context}


def register(registry):
    """Register this tool with a TurboMCP registry."""
    registry.register(__FUNC__, name=__NAME_JSON__, description=__DESC_JSON__)
    return __FUNC__
'''


def _generate_tool_code(skill):
    """Generate a Python tool module that actually executes its steps."""
    name = skill["name"]
    description = skill["description"].replace("\n", " ")
    steps_formatted = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(skill["steps"]))
    doc = (
        f"Auto-generated tool from skill: {name}\n{description}\n\nSteps:\n{steps_formatted}\n\n"
        f"Tools needed: {', '.join(skill['tools_needed'])}"
    )
    values = {
        "__DOC__": json.dumps(doc),
        "__FUNC__": re.sub(r"[^a-z0-9_]", "_", name.lower()),
        "__NAME__": name,
        "__NAME_JSON__": json.dumps(name),
        "__DESC__": description.replace("\\", " ").replace('"', "'"),
        "__DESC_JSON__": json.dumps(description),
        "__TOOLS__": json.dumps(skill["tools_needed"]),
        "__STEPS__": json.dumps(skill["steps"]),
    }
    code = TOOL_TEMPLATE
    # Longest keys first so __NAME_JSON__ is not clobbered by __NAME__.
    for key in sorted(values, key=len, reverse=True):
        code = code.replace(key, values[key])
    return code


# ---------- smoke test ----------


def _validate_generated_code(code):
    """Reject generated modules with direct escape hatches before import."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"syntax error: {e}"

    blocked_modules = {"os", "subprocess", "shutil", "socket", "ctypes", "multiprocessing"}
    blocked_calls = {"eval", "exec", "compile", "__import__", "system", "popen", "remove", "unlink"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".", 1)[0] in blocked_modules:
                    return f"blocked import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".", 1)[0] in blocked_modules:
                return f"blocked import: {node.module}"
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in blocked_calls:
                return f"blocked call: {node.func.id}"
            if isinstance(node.func, ast.Attribute) and node.func.attr in blocked_calls:
                return f"blocked call: {node.func.attr}"
    return ""

def _smoke_test(tool_path, skill_name):
    """Import the generated module and call its register function."""
    try:
        validation_error = _validate_generated_code(tool_path.read_text(encoding="utf-8"))
        if validation_error:
            return False, validation_error
        spec = importlib.util.spec_from_file_location(skill_name, tool_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # Test registration
        from turbomcp import TurboMCP
        test_reg = TurboMCP()
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
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", skill["name"]):
            results["errors"].append(f"{skill_path.name}: unsafe skill name")
            continue
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
        _git_commit(mind.parent, f"codify skills: generated {results['generated']} tools", [tools_dir])

    return results


if __name__ == "__main__":
    import json
    result = codify_skills()
    print(json.dumps(result, indent=2))
