"""Static scanners for provider-specific Mini and React implementation facts."""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from graph_ba.config import ProjectConfig, classify_id, normalize_id

import json
from .models import MiniAdminComponentTrace
from .scanning import scan_test_references


@dataclass
class MiniRegistryTrace:
    """A trace edge declared on a mini Resource or CustomMethod."""

    source_id: str
    source_type: str
    title: str
    source_file: Path
    line_number: int
    target_id: str
    relation_type: str
    context: str = ""
    rel_path: str = ""


@dataclass
class ReactUiElement:
    """A React JSX UI trace attribute selected for graph import."""

    source_id: str
    source_type: str
    source_file: Path
    line_number: int
    selector: str
    target_id: str
    target_type: str
    relation_type: str
    role: str = "component"
    context: str = ""
    rel_path: str = ""


@dataclass
class MiniAdminSourceTrace:
    """A data-source dependency declared by a mini-admin custom screen."""

    source_id: str
    source_type: str
    title: str
    source_file: Path
    line_number: int
    target_id: str
    target_type: str
    relation_type: str
    context: str = ""
    rel_path: str = ""


def scan_mini_registry_traces(
    root: Path,
    config: ProjectConfig,
) -> list[MiniRegistryTrace]:
    """Scan mini Resource(...) and CustomMethod(...) trace declarations.

    This is intentionally static AST parsing. graph-ba must not import a mini
    application just to learn its trace contract.
    """
    if not config.mini_registry:
        return []

    files: list[Path] = []
    for dir_str in config.mini_registry.dirs:
        d = root / dir_str
        if not d.exists():
            continue
        for ext in config.mini_registry.extensions:
            files.extend(sorted(d.rglob(f"*.{ext}")))
    for pattern in config.mini_registry.files:
        files.extend(sorted(root.glob(pattern)))

    seen: set[Path] = set()
    traces: list[MiniRegistryTrace] = []
    for filepath in files:
        if filepath in seen or not filepath.is_file():
            continue
        seen.add(filepath)
        try:
            tree = ast.parse(filepath.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        try:
            rel_path = str(filepath.relative_to(root))
        except ValueError:
            rel_path = str(filepath)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call_name = _ast_call_name(node.func)
            if call_name == "Resource":
                source_key = _ast_kw_string(node, "name")
                title = _ast_kw_string(node, "title") or source_key or ""
                source_type = config.mini_registry.resource_type
                source_id = f"{source_type}:{source_key}" if source_key else ""
            elif call_name == "CustomMethod":
                source_key = _ast_kw_string(node, "code")
                title = _ast_kw_string(node, "title") or source_key or ""
                source_type = config.mini_registry.custom_method_type
                source_id = f"{source_type}:{source_key}" if source_key else ""
            else:
                continue
            if not source_id:
                continue

            links = _extract_trace_links(_ast_kw_value(node, "trace"))
            if not links:
                traces.append(
                    MiniRegistryTrace(
                        source_id=source_id,
                        source_type=source_type,
                        title=title,
                        source_file=filepath,
                        line_number=getattr(node, "lineno", 0),
                        target_id="",
                        relation_type="",
                        context="mini_registry",
                        rel_path=rel_path,
                    )
                )
                continue

            for raw_target, raw_relation in links:
                target_id = normalize_id(raw_target, config)
                if classify_id(target_id, config) is None:
                    continue
                traces.append(
                    MiniRegistryTrace(
                        source_id=source_id,
                        source_type=source_type,
                        title=title,
                        source_file=filepath,
                        line_number=getattr(node, "lineno", 0),
                        target_id=target_id,
                        relation_type=_relation_type(raw_relation),
                        context="mini_registry_trace",
                        rel_path=rel_path,
                    )
                )
    return traces


def scan_mini_admin_source_traces(
    root: Path,
    config: ProjectConfig,
) -> list[MiniAdminSourceTrace]:
    """Scan mini-admin `sources.ts` metadata into screen data dependencies.

    The scanner is intentionally static and literal-only. It understands the
    local source metadata style:
    - `resource: SOME_RESOURCES.key`
    - `derivedFrom: [SOME_RESOURCES.key, "resource_name"]`
    - `meta: { customMethods: ["method.code"] }`
    """
    if not config.mini_admin_sources:
        return []

    files: list[Path] = []
    for dir_str in config.mini_admin_sources.dirs:
        d = root / dir_str
        if not d.exists():
            continue
        for ext in config.mini_admin_sources.extensions:
            files.extend(sorted(d.rglob(f"*.{ext}")))
    for pattern in config.mini_admin_sources.files:
        files.extend(sorted(root.glob(pattern)))

    seen: set[Path] = set()
    traces: list[MiniAdminSourceTrace] = []
    for filepath in files:
        if filepath in seen or not filepath.is_file():
            continue
        seen.add(filepath)
        try:
            text = filepath.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        try:
            rel_path = str(filepath.relative_to(root))
        except ValueError:
            rel_path = str(filepath)

        screen_id = _mini_admin_screen_id(filepath, root, config)
        if not screen_id:
            continue
        screen_type = classify_id(screen_id, config) or "SCREEN_FAMILY"
        constants_text = _read_sibling_ts_constants(filepath) + "\n" + text
        resource_constants = _extract_ts_resource_constants(constants_text)
        resources = _extract_ts_resources(text, resource_constants)
        custom_methods = _extract_ts_custom_methods(text)
        source_map = _extract_mini_admin_source_map(text, resource_constants)
        frontend_computed_sources = [
            key for key, value in source_map.items() if value.get("frontend_computed")
        ]
        title = screen_id

        for resource in sorted(resources):
            traces.append(
                MiniAdminSourceTrace(
                    source_id=screen_id,
                    source_type=screen_type,
                    title=title,
                    source_file=filepath,
                    line_number=_line_number_for(text, resource),
                    target_id=f"{config.mini_admin_sources.resource_type}:{resource}",
                    target_type=config.mini_admin_sources.resource_type,
                    relation_type=config.mini_admin_sources.relation_type,
                    context="mini_admin_sources",
                    rel_path=rel_path,
                )
            )
        for method_code in sorted(custom_methods):
            traces.append(
                MiniAdminSourceTrace(
                    source_id=screen_id,
                    source_type=screen_type,
                    title=title,
                    source_file=filepath,
                    line_number=_line_number_for(text, method_code),
                    target_id=f"{config.mini_admin_sources.custom_method_type}:{method_code}",
                    target_type=config.mini_admin_sources.custom_method_type,
                    relation_type=config.mini_admin_sources.relation_type,
                    context="mini_admin_sources",
                    rel_path=rel_path,
                )
            )
        for source_key in sorted(frontend_computed_sources):
            target_id = _frontend_computed_id(screen_id, source_key, config)
            traces.append(
                MiniAdminSourceTrace(
                    source_id=screen_id,
                    source_type=screen_type,
                    title=title,
                    source_file=filepath,
                    line_number=_line_number_for(text, source_key),
                    target_id=target_id,
                    target_type=config.mini_admin_sources.frontend_computed_type,
                    relation_type=config.mini_admin_sources.relation_type,
                    context="mini_admin_sources",
                    rel_path=rel_path,
                )
            )
    return traces


def _mini_admin_screen_id(filepath: Path, root: Path, config: ProjectConfig) -> str:
    if not config.mini_admin_sources:
        return ""
    try:
        parts = filepath.relative_to(root).parts
    except ValueError:
        parts = filepath.parts
    marker = config.mini_admin_sources.feature_path_segment
    if marker not in parts:
        return ""
    index = parts.index(marker)
    if index + 1 >= len(parts):
        return ""
    feature = parts[index + 1]
    screen_key = re.sub(r"[^A-Za-z0-9]+", "-", feature).strip("-").upper()
    return (
        f"{config.mini_admin_sources.screen_family_prefix}{screen_key}"
        if screen_key
        else ""
    )


def _collect_mini_admin_source_files(root: Path, config: ProjectConfig) -> list[Path]:
    if not config.mini_admin_sources:
        return []
    return _collect_configured_files(
        root,
        config.mini_admin_sources.dirs,
        config.mini_admin_sources.files,
        config.mini_admin_sources.extensions,
    )


def _collect_react_ui_aliases(
    root: Path,
    config: ProjectConfig,
) -> dict[str, set[str]]:
    """Map ordinary data-testid selectors to UIC aliases found on the same JSX tag."""
    aliases: dict[str, set[str]] = defaultdict(set)
    if not config.react_ui:
        return aliases
    files = _collect_configured_files(
        root,
        config.react_ui.dirs,
        config.react_ui.files,
        config.react_ui.extensions,
    )
    attr_re = re.compile(
        r"(?P<prop>data-testid|data-test-id|testID|data-uic-id)\s*=\s*"
        r"(?:\"(?P<double>[^\"]+)\"|'(?P<single>[^']+)'|\{\s*\"(?P<braced_double>[^\"]+)\"\s*\}|\{\s*'(?P<braced_single>[^']+)'\s*\})"
    )
    for filepath in files:
        try:
            text = _mask_js_comments(filepath.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, OSError):
            continue
        for tag in re.finditer(r"<[A-Za-z][^<>]*?>", text, flags=re.DOTALL):
            attrs: dict[str, list[str]] = defaultdict(list)
            for match in attr_re.finditer(tag.group(0)):
                value = next(
                    item
                    for item in (
                        match.group("double"),
                        match.group("single"),
                        match.group("braced_double"),
                        match.group("braced_single"),
                    )
                    if item is not None
                )
                attrs[match.group("prop")].append(value)
            uic_values = [
                value
                for value in attrs.get("data-uic-id", [])
                if classify_id(normalize_id(value, config), config) == "UIC"
            ]
            test_values = (
                attrs.get("data-testid", [])
                + attrs.get("data-test-id", [])
                + attrs.get("testID", [])
            )
            for test_id in test_values:
                for uic in uic_values:
                    normalized_uic = normalize_id(uic, config)
                    aliases[test_id].add(normalized_uic)
                    aliases[normalized_uic].add(test_id)
    return aliases


def _component_aliases_for(selector: str, aliases: dict[str, set[str]]) -> list[str]:
    return sorted(aliases.get(selector, set()))


def _component_id_for(
    selector: str,
    aliases: list[str],
    config: ProjectConfig,
) -> str:
    for alias in aliases:
        normalized = normalize_id(alias, config)
        if classify_id(normalized, config) == "UIC":
            return normalized
    normalized = normalize_id(selector, config)
    if classify_id(normalized, config) == "UIC":
        return normalized
    prefix = (
        config.mini_admin_sources.component_fallback_prefix
        if config.mini_admin_sources
        else "UIC:"
    )
    return f"{prefix}{selector}"


def _trace_source_keys(raw_entry: dict[str, object]) -> list[str]:
    values: list[str] = []
    source = raw_entry.get("source")
    if isinstance(source, str):
        values.append(source)
    sources = raw_entry.get("sources")
    if isinstance(sources, list):
        values.extend(item for item in sources if isinstance(item, str))
    return list(dict.fromkeys(values))


def _trace_string_array(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _extract_mini_admin_source_map(
    text: str,
    constants: dict[str, dict[str, str]],
) -> dict[str, dict[str, object]]:
    body = _extract_first_ts_sources_object(text)
    if not body:
        return {}
    source_map: dict[str, dict[str, object]] = {}
    for key, value in _split_top_level_object_entries(body).items():
        source_map[key] = {
            "resources": _extract_ts_resources(value, constants),
            "custom_methods": _extract_ts_custom_methods(value),
            "frontend_computed": _is_frontend_computed_source(value),
        }
    return source_map


def _is_frontend_computed_source(value: str) -> bool:
    return (
        "computedFrontendDataSource" in value
        or re.search(r"\bkind\s*:\s*[\"']computed-frontend[\"']", value) is not None
    )


def _frontend_computed_id(
    screen_id: str, source_key: str, config: ProjectConfig
) -> str:
    node_type = (
        config.mini_admin_sources.frontend_computed_type
        if config.mini_admin_sources
        else "FRONTEND_COMPUTED"
    )
    safe_key = re.sub(r"[^A-Za-z0-9_.:-]+", "-", source_key).strip("-")
    return f"{node_type}:{screen_id}:{safe_key}"


def _extract_first_ts_sources_object(text: str) -> str:
    match = re.search(r"(?:export\s+)?const\s+\w*Sources\s*=\s*\{", text)
    if not match:
        return ""
    open_index = text.find("{", match.start())
    close_index = _find_matching_brace(text, open_index)
    if close_index < 0:
        return ""
    return text[open_index + 1 : close_index]


def _split_top_level_object_entries(body: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    index = 0
    while index < len(body):
        while index < len(body) and body[index] in " \t\r\n,":
            index += 1
        key_match = re.match(r"([A-Za-z_$][A-Za-z0-9_$]*)\s*:", body[index:])
        if not key_match:
            index += 1
            continue
        key = key_match.group(1)
        value_start = index + key_match.end()
        value_end = _find_top_level_comma_or_end(body, value_start)
        entries[key] = body[value_start:value_end].strip()
        index = value_end + 1
    return entries


def _find_matching_brace(text: str, open_index: int) -> int:
    depth = 0
    quote = ""
    escaped = False
    for index in range(open_index, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "\"'`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _find_top_level_comma_or_end(text: str, start: int) -> int:
    depths = {"{": 0, "[": 0, "(": 0}
    quote = ""
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "\"'`":
            quote = char
        elif char in depths:
            depths[char] += 1
        elif char == "}":
            if depths["{"] == 0 and depths["["] == 0 and depths["("] == 0:
                return index
            depths["{"] = max(0, depths["{"] - 1)
        elif char == "]":
            depths["["] = max(0, depths["["] - 1)
        elif char == ")":
            depths["("] = max(0, depths["("] - 1)
        elif char == "," and all(depth == 0 for depth in depths.values()):
            return index
    return len(text)


def _extract_ts_resource_constants(text: str) -> dict[str, dict[str, str]]:
    constants: dict[str, dict[str, str]] = {}
    pattern = re.compile(
        r"(?:export\s+)?const\s+([A-Z0-9_]*RESOURCES)\s*=\s*\{(?P<body>.*?)\}\s*as\s+const",
        re.DOTALL,
    )
    entry_pattern = re.compile(r"([A-Za-z0-9_]+)\s*:\s*[\"']([^\"']+)[\"']")
    for match in pattern.finditer(text):
        constants[match.group(1)] = {
            key: value for key, value in entry_pattern.findall(match.group("body"))
        }
    return constants


def _read_sibling_ts_constants(filepath: Path) -> str:
    chunks: list[str] = []
    for sibling_name in ("resources.ts", "resources.tsx"):
        sibling = filepath.with_name(sibling_name)
        if sibling == filepath or not sibling.is_file():
            continue
        try:
            chunks.append(sibling.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, OSError):
            continue
    return "\n".join(chunks)


def _extract_ts_resources(text: str, constants: dict[str, dict[str, str]]) -> set[str]:
    resources: set[str] = set()
    for raw in re.findall(r"\bresource\s*:\s*([^,\n}]+)", text):
        value = _resolve_ts_resource_expr(raw, constants)
        if value:
            resources.add(value)
    for raw_array in re.findall(
        r"\bderivedFrom\s*:\s*\[(.*?)\]", text, flags=re.DOTALL
    ):
        for raw_item in raw_array.split(","):
            value = _resolve_ts_resource_expr(raw_item, constants)
            if value and ":" not in value:
                resources.add(value)
    return resources


def _extract_ts_custom_methods(text: str) -> set[str]:
    methods: set[str] = set()
    for raw_array in re.findall(
        r"\bcustomMethods\s*:\s*\[(.*?)\]", text, flags=re.DOTALL
    ):
        for value in re.findall(r"[\"']([^\"']+)[\"']", raw_array):
            if value:
                methods.add(value)
    for value in re.findall(r"\bcustomMethod\s*:\s*[\"']([^\"']+)[\"']", text):
        if value:
            methods.add(value)
    return methods


def _resolve_ts_resource_expr(raw: str, constants: dict[str, dict[str, str]]) -> str:
    expr = raw.strip().rstrip(",")
    literal = re.match(r"^[\"']([^\"']+)[\"']$", expr)
    if literal:
        return literal.group(1)
    attr = re.match(r"^([A-Z0-9_]*RESOURCES)\.([A-Za-z0-9_]+)$", expr)
    if attr:
        return constants.get(attr.group(1), {}).get(attr.group(2), "")
    return ""


def _line_number_for(text: str, needle: str) -> int:
    index = text.find(needle)
    if index < 0:
        return 0
    return text.count("\n", 0, index) + 1


def scan_react_ui_elements(
    root: Path,
    config: ProjectConfig,
) -> list[ReactUiElement]:
    """Scan React JSX UI trace attributes.

    This scanner intentionally imports only literal attribute values selected by
    `[react_ui]` patterns unless `include_unmatched=true`.
    """
    if not config.react_ui:
        return []

    files = _collect_configured_files(
        root,
        config.react_ui.dirs,
        config.react_ui.files,
        config.react_ui.extensions,
    )
    prop_roles = _react_ui_prop_roles(config)
    prop_re = "|".join(re.escape(prop) for prop in prop_roles)
    if not prop_re:
        return []
    attr_re = re.compile(
        rf"(?P<prop>{prop_re})\s*=\s*"
        r"(?:"
        r"\"(?P<double>[^\"]+)\""
        r"|'(?P<single>[^']+)'"
        r"|\{\s*\"(?P<braced_double>[^\"]+)\"\s*\}"
        r"|\{\s*'(?P<braced_single>[^']+)'\s*\}"
        r"|\{\s*`(?P<braced_template>[^`$]+)`\s*\}"
        r")"
    )
    component_include_res = [
        re.compile(pattern) for pattern in config.react_ui.include_patterns
    ]
    screen_include_res = [
        re.compile(pattern) for pattern in config.react_ui.screen_include_patterns
    ]
    screen_family_include_res = [
        re.compile(pattern)
        for pattern in config.react_ui.screen_family_include_patterns
    ]

    elements: list[ReactUiElement] = []
    for filepath in files:
        try:
            text = filepath.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        masked = _mask_js_comments(text)
        try:
            rel_path = str(filepath.relative_to(root))
        except ValueError:
            rel_path = str(filepath)
        source_id = f"REACT:{rel_path}"

        for match in attr_re.finditer(masked):
            selector = next(
                value
                for value in (
                    match.group("double"),
                    match.group("single"),
                    match.group("braced_double"),
                    match.group("braced_single"),
                    match.group("braced_template"),
                )
                if value is not None
            )
            role = prop_roles[match.group("prop")]
            if role == "screen_family":
                include_res = screen_family_include_res
                fallback_type = config.react_ui.screen_family_type
                relation_type = config.react_ui.screen_relation_type
            elif role == "screen":
                include_res = screen_include_res
                fallback_type = config.react_ui.screen_type
                relation_type = config.react_ui.screen_relation_type
            else:
                include_res = component_include_res
                fallback_type = config.react_ui.selector_type
                relation_type = config.react_ui.relation_type

            if not _react_selector_included(
                selector,
                include_res,
                config.react_ui.include_unmatched,
            ):
                continue

            normalized = normalize_id(selector, config)
            target_type = classify_id(normalized, config)
            target_id = normalized
            if target_type is None:
                if not config.react_ui.include_unmatched:
                    continue
                target_type = fallback_type
                target_id = f"{target_type}:{selector}"

            line_number = masked.count("\n", 0, match.start()) + 1
            elements.append(
                ReactUiElement(
                    source_id=source_id,
                    source_type=config.react_ui.source_type,
                    source_file=filepath,
                    line_number=line_number,
                    selector=selector,
                    target_id=target_id,
                    target_type=target_type,
                    relation_type=relation_type,
                    role=role,
                    context=f"{match.group('prop')}={selector}",
                    rel_path=rel_path,
                )
            )
    return elements


def _react_ui_prop_roles(config: ProjectConfig) -> dict[str, str]:
    if not config.react_ui:
        return {}
    roles: dict[str, str] = {}
    for prop in config.react_ui.props:
        roles[prop] = "component"
    for prop in config.react_ui.screen_props:
        roles[prop] = "screen"
    for prop in config.react_ui.screen_family_props:
        roles[prop] = "screen_family"
    return roles


def _collect_configured_files(
    root: Path,
    dirs: list[str],
    files: list[str],
    extensions: list[str],
) -> list[Path]:
    collected: list[Path] = []
    for dir_str in dirs:
        d = root / dir_str
        if not d.exists():
            continue
        for ext in extensions:
            collected.extend(sorted(d.rglob(f"*.{ext}")))
    for pattern in files:
        collected.extend(sorted(root.glob(pattern)))
    seen: set[Path] = set()
    result: list[Path] = []
    for item in collected:
        if item in seen or not item.is_file():
            continue
        seen.add(item)
        result.append(item)
    return result


def _react_selector_included(
    selector: str,
    include_res: list[re.Pattern],
    include_unmatched: bool,
) -> bool:
    if include_unmatched:
        return True
    return any(pattern.search(selector) for pattern in include_res)


def _mask_js_comments(text: str) -> str:
    """Replace JS/TS comments with spaces while preserving line offsets."""
    result: list[str] = []
    i = 0
    state = "normal"
    quote = ""
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""

        if state == "line_comment":
            if ch == "\n":
                state = "normal"
                result.append(ch)
            else:
                result.append(" ")
            i += 1
            continue

        if state == "block_comment":
            if ch == "*" and nxt == "/":
                result.extend("  ")
                state = "normal"
                i += 2
            else:
                result.append("\n" if ch == "\n" else " ")
                i += 1
            continue

        if state == "string":
            result.append(ch)
            if ch == "\\" and i + 1 < len(text):
                result.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                state = "normal"
            i += 1
            continue

        if ch in {"'", '"', "`"}:
            state = "string"
            quote = ch
            result.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            result.extend("  ")
            state = "line_comment"
            i += 2
            continue
        if ch == "/" and nxt == "*":
            result.extend("  ")
            state = "block_comment"
            i += 2
            continue
        result.append(ch)
        i += 1
    return "".join(result)


def _ast_call_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _ast_kw_value(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _ast_kw_string(call: ast.Call, name: str) -> str | None:
    value = _ast_kw_value(call, name)
    return (
        value.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
        else None
    )


def _ast_string(value: ast.expr | None) -> str | None:
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    return None


def _ast_string_items(value: ast.expr | None) -> list[str]:
    if isinstance(value, (ast.Tuple, ast.List, ast.Set)):
        return [
            item.value
            for item in value.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        ]
    single = _ast_string(value)
    return [single] if single else []


def _extract_trace_links(value: ast.expr | None) -> list[tuple[str, str]]:
    if value is None:
        return []
    if not isinstance(value, ast.Call):
        return []
    call_name = _ast_call_name(value.func)

    # Traceability.implements("AC-...")
    if (
        isinstance(value.func, ast.Attribute)
        and value.func.attr == "implements"
        and _ast_call_name(value.func.value) == "Traceability"
    ):
        artifacts = [
            artifact
            for artifact in (_ast_string(arg) for arg in value.args)
            if artifact is not None
        ]
        return [(artifact, "implements") for artifact in artifacts]

    if call_name != "Traceability":
        return []

    links: list[tuple[str, str]] = []
    artifacts_value = _ast_kw_value(value, "artifacts")
    for artifact in _ast_string_items(artifacts_value):
        links.append((artifact, "implements"))

    links_value = _ast_kw_value(value, "links")
    if isinstance(links_value, (ast.Tuple, ast.List, ast.Set)):
        for item in links_value.elts:
            parsed = _extract_trace_link(item)
            if parsed:
                links.append(parsed)
    return links


def _extract_trace_link(value: ast.expr) -> tuple[str, str] | None:
    if not isinstance(value, ast.Call) or _ast_call_name(value.func) != "TraceLink":
        return None
    artifact = _ast_kw_string(value, "artifact")
    if artifact is None and value.args:
        artifact = _ast_string(value.args[0])
    relation = _ast_kw_string(value, "relation")
    if relation is None and len(value.args) > 1:
        relation = _ast_string(value.args[1])
    if not artifact:
        return None
    return artifact, relation or "implements"


def _relation_type(raw: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "_", raw.strip()).strip("_").upper()
    return value or "IMPLEMENTS"


def scan_mini_admin_component_traces(
    root: Path,
    config: ProjectConfig,
) -> list[MiniAdminComponentTrace]:
    """Scan mini-admin component trace sidecars into component-level graph edges.

    This ties UI ids to canonical/raw acceptance ids and to the data-source
    contracts declared in the sibling `sources.ts`.
    """
    entries = build_mini_admin_component_trace_entries(root, config)
    traces: list[MiniAdminComponentTrace] = []
    for entry in entries:
        source_file = root / entry["rel_path"]
        line_number = int(entry.get("line_number", 0) or 0)
        component_id = str(entry["component_id"])
        screen_id = str(entry["screen_id"])
        selector = str(entry["selector"])
        for target_id in entry.get("ac_ids", []):
            traces.append(
                MiniAdminComponentTrace(
                    screen_id=screen_id,
                    component_id=component_id,
                    component_selector=selector,
                    source_file=source_file,
                    line_number=line_number,
                    target_id=str(target_id),
                    target_type=classify_id(str(target_id), config) or "AC",
                    relation_type=(
                        config.mini_admin_sources.trace_relation_type
                        if config.mini_admin_sources
                        else "TRACES_TO"
                    ),
                    context="mini_admin_component_trace:AC",
                    rel_path=str(entry["rel_path"]),
                )
            )
        for target_id in entry.get("raw_ids", []):
            traces.append(
                MiniAdminComponentTrace(
                    screen_id=screen_id,
                    component_id=component_id,
                    component_selector=selector,
                    source_file=source_file,
                    line_number=line_number,
                    target_id=str(target_id),
                    target_type=classify_id(str(target_id), config) or "RAC",
                    relation_type=(
                        config.mini_admin_sources.trace_relation_type
                        if config.mini_admin_sources
                        else "TRACES_TO"
                    ),
                    context="mini_admin_component_trace:RAC",
                    rel_path=str(entry["rel_path"]),
                )
            )
        for target_id in entry.get("resource_ids", []):
            traces.append(
                MiniAdminComponentTrace(
                    screen_id=screen_id,
                    component_id=component_id,
                    component_selector=selector,
                    source_file=source_file,
                    line_number=line_number,
                    target_id=str(target_id),
                    target_type=(
                        config.mini_admin_sources.resource_type
                        if config.mini_admin_sources
                        else "CRUDL_RESOURCE"
                    ),
                    relation_type=(
                        config.mini_admin_sources.relation_type
                        if config.mini_admin_sources
                        else "DEPENDS_ON"
                    ),
                    context="mini_admin_component_trace:CRUDL",
                    rel_path=str(entry["rel_path"]),
                )
            )
        for target_id in entry.get("custom_method_ids", []):
            traces.append(
                MiniAdminComponentTrace(
                    screen_id=screen_id,
                    component_id=component_id,
                    component_selector=selector,
                    source_file=source_file,
                    line_number=line_number,
                    target_id=str(target_id),
                    target_type=(
                        config.mini_admin_sources.custom_method_type
                        if config.mini_admin_sources
                        else "CUSTOM_METHOD"
                    ),
                    relation_type=(
                        config.mini_admin_sources.relation_type
                        if config.mini_admin_sources
                        else "DEPENDS_ON"
                    ),
                    context="mini_admin_component_trace:CUSTOM_METHOD",
                    rel_path=str(entry["rel_path"]),
                )
            )
        for target_id in entry.get("frontend_computed_ids", []):
            traces.append(
                MiniAdminComponentTrace(
                    screen_id=screen_id,
                    component_id=component_id,
                    component_selector=selector,
                    source_file=source_file,
                    line_number=line_number,
                    target_id=str(target_id),
                    target_type=(
                        config.mini_admin_sources.frontend_computed_type
                        if config.mini_admin_sources
                        else "FRONTEND_COMPUTED"
                    ),
                    relation_type=(
                        config.mini_admin_sources.relation_type
                        if config.mini_admin_sources
                        else "DEPENDS_ON"
                    ),
                    context="mini_admin_component_trace:FRONTEND_COMPUTED",
                    rel_path=str(entry["rel_path"]),
                )
            )
    return traces


def build_mini_admin_component_trace_entries(
    root: Path,
    config: ProjectConfig,
) -> list[dict[str, object]]:
    """Build graph-ba-owned runtime trace entries for mini-admin component ids."""
    if not config.mini_admin_sources:
        return []

    files = _collect_mini_admin_source_files(root, config)
    test_ids_by_ac = _collect_test_ids_by_ac(root, config)
    react_aliases = _collect_react_ui_aliases(root, config)
    entries: list[dict[str, object]] = []
    for filepath in files:
        try:
            text = filepath.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        try:
            rel_path = str(filepath.relative_to(root))
        except ValueError:
            rel_path = str(filepath)
        screen_id = _mini_admin_screen_id(filepath, root, config)
        if not screen_id:
            continue
        constants_text = _read_sibling_ts_constants(filepath) + "\n" + text
        resource_constants = _extract_ts_resource_constants(constants_text)
        source_map = _extract_mini_admin_source_map(text, resource_constants)
        trace_path = filepath.with_name(config.mini_admin_sources.trace_filename)
        if not trace_path.is_file():
            continue
        try:
            raw_trace = json.loads(trace_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue
        if not isinstance(raw_trace, dict):
            continue
        try:
            trace_rel_path = str(trace_path.relative_to(root))
        except ValueError:
            trace_rel_path = str(trace_path)
        for selector, raw_entry in raw_trace.items():
            if str(selector).startswith("$") or not isinstance(raw_entry, dict):
                continue
            source_keys = _trace_source_keys(raw_entry)
            source_contracts = [
                _source
                for key in source_keys
                for _source in [source_map.get(key)]
                if _source
            ]
            ac_ids = _trace_string_array(raw_entry.get("acIds"))
            raw_ids = _trace_string_array(raw_entry.get("rawIds"))
            aliases = _component_aliases_for(str(selector), react_aliases)
            component_id = _component_id_for(str(selector), aliases, config)
            test_ids = sorted(
                {
                    test_id
                    for ac_id in ac_ids
                    for test_id in test_ids_by_ac.get(ac_id, set())
                }
                | set(_trace_string_array(raw_entry.get("testIds")))
            )
            entries.append(
                {
                    "screen_id": screen_id,
                    "component_id": component_id,
                    "selector": str(selector),
                    "aliases": aliases,
                    "source_keys": source_keys,
                    "ac_ids": ac_ids,
                    "raw_ids": raw_ids,
                    "test_ids": test_ids,
                    "trace_gap": str(raw_entry.get("traceGap") or ""),
                    "resource_ids": sorted(
                        {
                            f"{config.mini_admin_sources.resource_type}:{resource}"
                            for source in source_contracts
                            for resource in source["resources"]
                        }
                    ),
                    "custom_method_ids": sorted(
                        {
                            f"{config.mini_admin_sources.custom_method_type}:{method}"
                            for source in source_contracts
                            for method in source["custom_methods"]
                        }
                    ),
                    "frontend_computed_ids": sorted(
                        {
                            _frontend_computed_id(screen_id, source_key, config)
                            for source_key in source_keys
                            for source in [source_map.get(source_key)]
                            if source and source.get("frontend_computed")
                        }
                    ),
                    "line_number": _line_number_for(text, str(selector)),
                    "rel_path": trace_rel_path,
                }
            )
    return entries


def export_mini_admin_component_trace_map(
    root: Path,
    config: ProjectConfig,
) -> dict[str, dict[str, dict[str, object]]]:
    """Return mini-admin overlay trace maps grouped by screen family id."""
    grouped: dict[str, dict[str, dict[str, object]]] = defaultdict(dict)
    for entry in build_mini_admin_component_trace_entries(root, config):
        screen_id = str(entry["screen_id"])
        payload = {
            "sources": entry.get("source_keys", []),
            "acIds": entry.get("ac_ids", []),
            "rawIds": entry.get("raw_ids", []),
            "testIds": entry.get("test_ids", []),
        }
        if entry.get("trace_gap"):
            payload["traceGap"] = entry["trace_gap"]
        keys = [
            str(entry["selector"]),
            *[str(alias) for alias in entry.get("aliases", [])],
        ]
        for key in dict.fromkeys(keys):
            grouped[screen_id][key] = payload
    return {screen: dict(entries) for screen, entries in sorted(grouped.items())}


def _collect_test_ids_by_ac(root: Path, config: ProjectConfig) -> dict[str, set[str]]:
    by_ac: dict[str, set[str]] = defaultdict(set)
    if not config.tests:
        return by_ac
    for ref in scan_test_references(root, config):
        test_id = f"TEST:{ref.rel_path}"
        for target_id in ref.target_ids:
            if classify_id(target_id, config) == "AC":
                by_ac[target_id].add(test_id)
    return by_ac
