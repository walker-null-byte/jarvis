"""Text templating for agents: action prompts and export files.

Action prompts use single braces: {input}, {settings.key}, {records.<table>.count}.
Anything else in braces is left exactly as written (prompts may contain JSON).

Export templates use a tiny mustache-like language. It only substitutes values;
it can never run code:

    {{field}}  {{settings.key}}  {{agent.name}}  {{date}}  {{@number}}
    {{#each table}} … {{/each}}             one block per record
    {{#each table status=new,contacted}}    only records whose field matches
    {{#if field}} … {{else}} … {{/if}}      truthy test (non-empty)
    {{count table}}                          number of records
Nothing is escaped: the output is markdown or plain text.
"""

import csv
import datetime
import io
import json
import re


class TemplateError(ValueError):
    pass


# ---------- action prompts ----------

PROMPT_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\}")


def _text(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(_text(v) for v in value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def render_prompt(template, inputs, settings, counts):
    """Fill {key}, {settings.key} and {records.table.count}; unknown names stay as written."""
    def replace(match):
        name = match.group(1)
        head, _, rest = name.partition(".")
        if head == "settings" and rest in settings:
            return _text(settings[rest])
        if head == "records" and rest.endswith(".count") and rest[:-6] in counts:
            return str(counts[rest[:-6]])
        if "." not in name and name in inputs:
            return _text(inputs[name])
        return match.group(0)
    return PROMPT_RE.sub(replace, template)


# ---------- export template engine ----------

TAG_RE = re.compile(r"\{\{\s*([#/]?)\s*([^{}]*?)\s*\}\}")
NAME_RE = re.compile(r"^@?[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)*$")


def parse_template(source):
    """Parse into a tree: list of str | ("var", name) | ("each", table, filters, body) | ("if", name, yes, no) | ("count", table)."""
    root = []
    stack = [("root", root, None)]  # (kind, current list, node)
    pos = 0
    for match in TAG_RE.finditer(source):
        stack[-1][1].append(source[pos:match.start()])
        pos = match.end()
        sigil, body = match.group(1), match.group(2)
        line = source.count("\n", 0, match.start()) + 1
        words = body.split()
        if sigil == "#":
            if not words or words[0] not in ("each", "if"):
                raise TemplateError(f"line {line}: unknown block {{{{#{body}}}}} (use #each or #if)")
            if len(words) < 2 or not NAME_RE.match(words[1]):
                raise TemplateError(f"line {line}: {{{{#{words[0]}}}}} needs a name, like {{{{#{words[0]} leads}}}}")
            if words[0] == "each":
                filters = {}
                for word in words[2:]:
                    key, eq, value = word.partition("=")
                    if not eq or not NAME_RE.match(key):
                        raise TemplateError(f"line {line}: filter '{word}' should look like status=new")
                    filters[key] = [v for v in value.split(",") if v]
                node = ["each", words[1], filters, []]
                stack[-1][1].append(node)
                stack.append(("each", node[3], node))
            else:
                node = ["if", words[1], [], []]
                stack[-1][1].append(node)
                stack.append(("if", node[2], node))
        elif sigil == "/":
            if len(stack) == 1 or stack[-1][0] != body.strip():
                expected = stack[-1][0] if len(stack) > 1 else "nothing"
                raise TemplateError(f"line {line}: {{{{/{body}}}}} does not close anything (open block: {expected})")
            stack.pop()
        elif body == "else":
            if stack[-1][0] != "if":
                raise TemplateError(f"line {line}: {{{{else}}}} outside {{{{#if}}}}")
            _, _, node = stack.pop()
            stack.append(("if", node[3], node))
        elif len(words) == 2 and words[0] == "count" and NAME_RE.match(words[1]):
            stack[-1][1].append(["count", words[1]])
        elif NAME_RE.match(body):
            stack[-1][1].append(["var", body])
        else:
            raise TemplateError(f"line {line}: cannot understand {{{{{body}}}}}")
    stack[-1][1].append(source[pos:])
    if len(stack) > 1:
        raise TemplateError(f"{{{{#{stack[-1][0]}}}}} is never closed (add {{{{/{stack[-1][0]}}}}})")
    return root


def _lookup(name, scopes):
    for scope in reversed(scopes):
        value = scope
        ok = True
        for part in name.split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                ok = False
                break
        if ok:
            return value
    return None


def _matches(record, filters):
    for key, allowed in filters.items():
        value = _text(record.get(key)).lower()
        if allowed and value not in [a.lower() for a in allowed]:
            return False
    return True


def _render(nodes, scopes, tables, out):
    for node in nodes:
        if isinstance(node, str):
            out.append(node)
        elif node[0] == "var":
            out.append(_text(_lookup(node[1], scopes)))
        elif node[0] == "count":
            out.append(str(len(tables.get(node[1], []))))
        elif node[0] == "if":
            value = _lookup(node[1], scopes)
            if node[1] in tables:
                value = tables[node[1]]
            _render(node[2] if value not in (None, "", [], {}, False, 0) else node[3], scopes, tables, out)
        elif node[0] == "each":
            rows = [r for r in tables.get(node[1], []) if _matches(r, node[2])]
            for i, row in enumerate(rows):
                _render(node[3], scopes + [{**row, "@index": i, "@number": i + 1}], tables, out)


def render_template(source, context, tables):
    """Render an export template. context: {settings, agent, date}; tables: {name: [records]}."""
    out = []
    _render(parse_template(source), [context], tables, out)
    text = "".join(out)
    return re.sub(r"\n{3,}", "\n\n", text)  # blocks leave blank lines behind


# ---------- export formats ----------

def _cell(value):
    if isinstance(value, (dict, list)) and not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
        return json.dumps(value, ensure_ascii=False)
    return _text(value)


def export_columns(table):
    return [f["name"] for f in table["fields"]] + ["created", "updated"]


def to_csv(table, records):
    """UTF-8 CSV with a BOM so Excel opens it with the right encoding."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    columns = export_columns(table)
    writer.writerow(columns)
    for record in records:
        writer.writerow([_csv_safe(_cell(record.get(c))) for c in columns])
    return "\ufeff" + buffer.getvalue()


def _csv_safe(text):
    """Defuse spreadsheet formulas (=cmd|..., +1+1) that a web page could have smuggled in."""
    return "'" + text if text[:1] in ("=", "+", "-", "@") and not re.match(r"^[-+]?\d", text) else text


def to_markdown(table, records):
    columns = [f["name"] for f in table["fields"] if not f.get("hidden")]
    labels = [f["label"] for f in table["fields"] if not f.get("hidden")]
    clean = lambda v: _cell(v).replace("|", "\\|").replace("\r", "").replace("\n", " ")  # noqa: E731
    lines = [f"# {table['label']}", "", "| " + " | ".join(labels) + " |", "|" + "---|" * len(labels)]
    lines += ["| " + " | ".join(clean(r.get(c)) for c in columns) + " |" for r in records]
    return "\n".join(lines) + "\n"


def to_json(tables):
    return json.dumps(tables, indent=2, ensure_ascii=False) + "\n"


def today():
    return datetime.date.today().isoformat()
