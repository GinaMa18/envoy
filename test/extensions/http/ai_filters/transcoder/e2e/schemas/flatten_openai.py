"""Flatten OpenAI CreateChatCompletionRequest to value-holding fields only (no container nodes).

Output: openai_fields.json  {path: {"type", "enum", "description"}}
        openai_fields.md    table for review
"""
import json, yaml, pathlib

HERE = pathlib.Path(__file__).parent
spec = yaml.safe_load(open(HERE / "openai_openapi.yaml"))
schemas = spec["components"]["schemas"]


def deref(s):
    while isinstance(s, dict) and "$ref" in s:
        s = schemas[s["$ref"].split("/")[-1]]
    return s


def is_object(s):
    return isinstance(s, dict) and (s.get("type") == "object" or "properties" in s
                                    or any(k in s for k in ("allOf", "anyOf", "oneOf")))


LEAVES = {}      # path -> info
CONTAINERS = []  # paths dropped because they only hold children


def add_leaf(path, s, via=None):
    s = deref(s)
    info = LEAVES.setdefault(path, {"type": set(), "enum": set(), "description": ""})
    # A union member list: merge the members' types and enums.
    for key in ("allOf", "anyOf", "oneOf"):
        for m in s.get(key, []):
            add_leaf(path, m)
    t = s.get("type")
    if isinstance(t, list):
        info["type"].update(t)
    elif t:
        info["type"].add(t)
    if "enum" in s:
        info["enum"].update(str(e) for e in s["enum"])
    if not info["description"] and s.get("description"):
        info["description"] = s["description"].strip().split("\n")[0][:110]


def walk(s, prefix, depth=0):
    s = deref(s)
    if depth > 7 or not isinstance(s, dict):
        return
    # Union members all contribute properties to the same prefix.
    for key in ("allOf", "anyOf", "oneOf"):
        for sub in s.get(key, []):
            sub = deref(sub)
            if is_object(sub) or sub.get("type") == "array":
                walk(sub, prefix, depth)
            elif prefix:
                add_leaf(prefix, sub)   # e.g. tool_choice: string | object
    if s.get("type") == "array":
        walk(s.get("items", {}), prefix + "[]", depth + 1)
        return
    props = s.get("properties") or {}
    # Free-form object (additionalProperties, no fixed children): it is a value field.
    if not props and s.get("type") == "object" and prefix:
        add_leaf(prefix, s)
        return
    for name, sub in props.items():
        path = f"{prefix}.{name}" if prefix else name
        sub_d = deref(sub)
        if not isinstance(sub_d, dict):
            add_leaf(path, {})
            continue
        if sub_d.get("type") == "array":
            items = deref(sub_d.get("items", {}))
            if is_object(items):
                CONTAINERS.append(path + "[]")
                walk(items, path + "[]", depth + 1)
            else:
                add_leaf(path + "[]", items)       # array of scalars: the element is the value
        elif is_object(sub_d) and any(deref(m).get("type") == "array" and not is_object(deref(deref(m).get("items", {})))
                                      for k in ("anyOf", "oneOf") for m in sub_d.get(k, [])) and not sub_d.get("properties"):
            # scalar | array-of-scalars (e.g. stop): one path per shape.
            for k in ("anyOf", "oneOf"):
                for m in sub_d.get(k, []):
                    m = deref(m)
                    if m.get("type") == "array":
                        add_leaf(path + "[]", deref(m.get("items", {})))
                    elif m.get("type") != "null":
                        add_leaf(path, m)
        elif is_object(sub_d):
            def member_has_children(m):
                m = deref(m)
                if m.get("type") == "array":
                    return is_object(deref(m.get("items", {})))
                return bool(m.get("properties")) or any(
                    member_has_children(x) for k in ("allOf", "anyOf", "oneOf") for x in m.get(k, []))
            has_children = bool(sub_d.get("properties")) or any(
                member_has_children(m) for k in ("allOf", "anyOf", "oneOf") for m in sub_d.get(k, []))
            if has_children:
                CONTAINERS.append(path)
                walk(sub_d, path, depth + 1)
                # A union like tool_choice: "auto" | {...} is also a value at the parent path.
                for k in ("anyOf", "oneOf"):
                    for m in sub_d.get(k, []):
                        m = deref(m)
                        if not is_object(m) and m.get("type") != "array":
                            add_leaf(path, m)
            else:
                add_leaf(path, sub_d)           # free-form object, e.g. metadata, logit_bias
        else:
            add_leaf(path, sub_d)


walk(schemas["CreateChatCompletionRequest"], "")

# A nullable container (`moderation: object | null`) leaves a null-only "leaf" at the container's
# own path. It holds no value; drop it.
containers = set(CONTAINERS)
for p in list(LEAVES):
    if p in containers and LEAVES[p]["type"] <= {"null"}:
        del LEAVES[p]

out = {p: {"type": sorted(i["type"]), "enum": sorted(i["enum"]), "description": i["description"]}
       for p, i in sorted(LEAVES.items())}
json.dump({"fields": out, "containers": sorted(set(CONTAINERS))}, open(HERE / "openai_fields.json", "w"), indent=1)

lines = ["# OpenAI `CreateChatCompletionRequest` — fields under test",
         "", f"Source: openai/openai-openapi `openapi.yaml` (spec {spec['info'].get('version')}). "
         f"**{len(out)} value-holding fields**; {len(set(CONTAINERS))} container nodes removed (listed at the end).",
         "", "| # | Field | Type | Enum / note |", "|---|---|---|---|"]
for n, (p, i) in enumerate(out.items(), 1):
    note = ", ".join(i["enum"]) if i["enum"] else i["description"]
    lines.append(f"| {n} | `{p}` | {' / '.join(i['type']) or '—'} | {note} |")
lines += ["", f"## Container nodes removed ({len(set(CONTAINERS))})", "",
          "Hold no value of their own; covered when any child arrives.", ""]
lines += [f"- `{c}`" for c in sorted(set(CONTAINERS))]
(HERE / "openai_fields.md").write_text("\n".join(lines) + "\n")
print(len(out), "fields;", len(set(CONTAINERS)), "containers")
