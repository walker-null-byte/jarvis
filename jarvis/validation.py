"""Small dependency-free validator for tool arguments."""


def validate_arguments(schema, arguments):
    if not schema:
        return ""
    if schema.get("type") != "object":
        return ""
    if not isinstance(arguments, dict):
        return "Arguments must be a JSON object"

    for name in schema.get("required", []):
        if name not in arguments:
            return f"Missing required argument: {name}"

    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            return f"Unknown argument(s): {', '.join(unknown)}"

    for name, value in arguments.items():
        rule = properties.get(name, {})
        error = _validate_value(name, value, rule)
        if error:
            return error
    return ""


def _validate_value(name, value, rule):
    expected = rule.get("type")
    if value is None or not expected:
        return ""
    valid = {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
    }.get(expected, True)
    if not valid:
        return f"Argument '{name}' must be of type {expected}"
    if "enum" in rule and value not in rule["enum"]:
        return f"Argument '{name}' must be one of: {', '.join(map(str, rule['enum']))}"
    return ""
