import importlib.util
spec = importlib.util.spec_from_file_location('check_disk_space', 'tools/check-disk-space.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

# Test just the prompt building and LLM call
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

available_tools = registry.to_openai_tools()
llm = LLM(cfg)

context = {}
context_summary = ''
tool_names = ', '.join(t['function']['name'] for t in available_tools)
prompt = (
    'You are executing step {step_num} of the "{name}" skill.\n'
    'Skill: {description}\n'
    'Step: {step}\n'
    'Available tools: {tool_names}\n'
    '{context_summary}\n\n'
    'Respond with ONLY a JSON object:\n'
    '{{\n'
    '  "tool": "tool_name",\n'
    '  "arguments": {{"arg": "value"}}\n'
    '}}\n'
    'Or if no tool is needed (e.g., pure computation), respond with:\n'
    '{{\n'
    '  "tool": null,\n'
    '  "result": "computed result"\n'
    '}}'
).format(
    step_num=1,
    name='check-disk-space',
    description='Check Disk Space',
    step='Run the `df -h /` command to get disk usage for the root filesystem',
    tool_names=tool_names,
    context_summary=context_summary
)

print('PROMPT:')
print(prompt)
print('---')

messages = [
    {'role': 'system', 'content': 'You are a tool executor. Output ONLY valid JSON.'},
    {'role': 'user', 'content': prompt}
]
response = llm.chat(messages, tools=available_tools)
content = (response.get('content') or '').strip()
print('RESPONSE:')
print(repr(content))

# Test regex
import re
match = re.search(r'\{.*\}', content, re.DOTALL)
if match:
    print('MATCH:', repr(match.group(0)))
else:
    print('NO MATCH')