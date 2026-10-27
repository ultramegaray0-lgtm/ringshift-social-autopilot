"""Small, careful edits to the single-file Ringshift game, for experiment branches only.

Nothing here ever writes to the game's `main` branch. The level lab and theme lab use these helpers to produce a
patched copy of the stable build, which is validated in a real browser and then offered as a pull request.

The game is one big HTML file with the code inline, so edits are anchored on structure, not line numbers:
* `find_object_end(source, "const LEVELS = {")` returns the index just after the `};` that closes LEVELS, using a
  scanner that understands JavaScript strings, template literals and comments (the level comments contain brackets).
* Inserted code is wrapped in START/END marker comments so a later run (or a person) can find, replace or remove it.
"""

import re


class PatchError(RuntimeError):
    pass


def _skip_string(source, index, quote):
    """Index just after the string literal starting at `index` (which holds the opening quote)."""
    i = index + 1
    while i < len(source):
        char = source[i]
        if char == "\\":
            i += 2
            continue
        if quote == "`" and char == "$" and source.startswith("${", i):
            i = _match_brace(source, i + 1) + 1
            continue
        if char == quote:
            return i + 1
        if char == "\n" and quote != "`":
            raise PatchError(f"unterminated string literal near index {index}")
        i += 1
    raise PatchError(f"unterminated string literal near index {index}")


def _match_brace(source, index):
    """Index of the bracket that closes the one at `index` ('{', '[' or '('), skipping strings and comments."""
    pairs = {"{": "}", "[": "]", "(": ")"}
    stack = [source[index]]
    if stack[0] not in pairs:
        raise PatchError(f"no opening bracket at index {index}")
    i = index + 1
    while i < len(source):
        char = source[i]
        if source.startswith("//", i):
            end = source.find("\n", i)
            i = len(source) if end == -1 else end + 1
            continue
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            if end == -1:
                raise PatchError("unterminated block comment")
            i = end + 2
            continue
        if char in "'\"`":
            i = _skip_string(source, i, char)
            continue
        if char in pairs:
            stack.append(char)
        elif char in pairs.values():
            if not stack or pairs[stack[-1]] != char:
                raise PatchError(f"mismatched {char!r} at index {i}")
            stack.pop()
            if not stack:
                return i
        i += 1
    raise PatchError("unbalanced brackets")


def find_object_end(source, declaration):
    """Index just after the statement `declaration ... };` (e.g. "const LEVELS = {"). Raises PatchError."""
    start = source.find(declaration)
    if start == -1:
        raise PatchError(f"{declaration!r} not found in the game source")
    if source.find(declaration, start + 1) != -1:
        raise PatchError(f"{declaration!r} appears more than once")
    brace = source.index("{", start)
    close = _match_brace(source, brace)
    rest = source[close + 1:close + 3]
    end = close + 1 + (1 if rest.startswith(";") else 0)
    return end


def marker_block(name, body):
    return (f"/*{name}_START -- added by ringshift-social-autopilot on an experiment branch; review before merging*/\n"
            f"{body.rstrip()}\n/*{name}_END*/")


def remove_block(source, name):
    """Remove a block added by insert_after / insert_before, restoring the source exactly."""
    pattern = re.compile(r"\n/\*" + re.escape(name) + r"_START.*?/\*" + re.escape(name) + r"_END\*/", re.S)
    return pattern.sub("", source)


def insert_after(source, declaration, name, body):
    """Insert a marked block right after the statement that `declaration` starts. An older block with the same name is
    replaced, so re-running never stacks copies."""
    source = remove_block(source, name)
    end = find_object_end(source, declaration)
    return source[:end] + "\n" + marker_block(name, body) + source[end:]


def insert_before(source, anchor, name, body):
    """Insert a marked block on its own lines right before the first occurrence of `anchor` (which must be unique and
    start a line)."""
    source = remove_block(source, name)
    index = source.find(anchor)
    if index == -1:
        raise PatchError(f"anchor {anchor!r} not found in the game source")
    if source.find(anchor, index + 1) != -1:
        raise PatchError(f"anchor {anchor!r} appears more than once")
    if index and source[index - 1] != "\n":
        raise PatchError(f"anchor {anchor!r} does not start a line")
    return source[:index] + marker_block(name, body) + "\n" + source[index:]


def js_literal(value, indent=""):
    """A compact JavaScript object literal in the style of the game's level data ({name:'X', slots:12, ...})."""
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            name = key if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", str(key)) else (str(key) if str(key).isdigit() else repr(str(key)))
            parts.append(f"{name}:{js_literal(item, indent)}")
        return "{" + ", ".join(parts) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(js_literal(item, indent) for item in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{text}'"
