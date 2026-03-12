import os
import re
from tree_sitter_language_pack import get_parser

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}
STATIC_EXTENSIONS = (
    ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".map"
)
SKIP_DIRS = {"test", "node_modules"}
DEFAULT_RESOURCE_GENERATORS = {"finale", "epilogue", "epilogue_js", "epiloguejs"}
DEFAULT_RESOURCE_LIST_METHODS   = ("GET", "POST")
DEFAULT_RESOURCE_DETAIL_METHODS = ("GET", "PUT", "PATCH", "DELETE")

def _extract_string_value_raw(source_code, node):
    return source_code[node.start_byte:node.end_byte].strip("\"'`")


def strategy_key_value_pair(key_name):
    """
    Returns an extractor that collects values of { <key_name>: 'StringValue' }
    pairs anywhere in the file.

    Usage:
        strategy_key_value_pair('name')   # { name: 'User', ... }
        strategy_key_value_pair('modelName')
    """
    def extractor(source_code, root_node):
        names = []
        def traverse(node):
            if node.type == "pair":
                k = node.child_by_field_name("key")
                v = node.child_by_field_name("value")
                if k and v and v.type == "string":
                    key_raw = source_code[k.start_byte:k.end_byte].strip("\"'`")
                    if key_raw == key_name:
                        names.append(_extract_string_value_raw(source_code, v))
            for child in node.children:
                traverse(child)
        traverse(root_node)
        # Deduplicate preserving order
        seen = set()
        result = []
        for n in names:
            if n not in seen:
                seen.add(n)
                result.append(n)
        return result
    extractor.__name__ = f"strategy_key_value_pair({key_name!r})"
    return extractor


def strategy_array_of_strings(var_name):
    """
    Returns an extractor that collects string values from a top-level
    array variable declaration.

    e.g.  const MODELS = ['User', 'Product', 'Order']
    Usage:
        strategy_array_of_strings('MODELS')
    """
    def extractor(source_code, root_node):
        names = []
        def traverse(node):
            if node.type == "variable_declarator":
                name_node  = node.child_by_field_name("name")
                value_node = node.child_by_field_name("value")
                if name_node and value_node and value_node.type == "array":
                    if source_code[name_node.start_byte:name_node.end_byte].strip() == var_name:
                        for item in value_node.children:
                            if item.type == "string":
                                names.append(_extract_string_value_raw(source_code, item))
            for child in node.children:
                traverse(child)
        traverse(root_node)
        return names
    extractor.__name__ = f"strategy_array_of_strings({var_name!r})"
    return extractor


def strategy_regex(pattern, group=1):
    """
    Returns an extractor that runs a regex over the raw source text
    and collects all matches of the given capture group.

    e.g.  strategy_regex(r"registerModel\\('(\\w+)'\\)")
    """
    compiled = re.compile(pattern)
    def extractor(source_code, root_node):
        matches = compiled.findall(source_code)
        if matches and isinstance(matches[0], tuple):
            matches = [m[group - 1] for m in matches]
        seen = set()
        result = []
        for m in matches:
            if m not in seen:
                seen.add(m)
                result.append(m)
        return result
    extractor.__name__ = f"strategy_regex({pattern!r})"
    return extractor

def _autodetect_strategy(source_code):
    """
    Inspect source_code for known patterns and return the best built-in
    model name extractor strategy, or None if nothing is recognised.

    Heuristics (checked in priority order):
      1. { name: '...' }  — finale/epilogue style object array
      2. { modelName: '...' }
      3. Array variable containing plain strings that look like model names
         (PascalCase words)
    """
    # Heuristic 1: { name: 'SomeModel' }
    if re.search(r"""[{,]\s*name\s*:\s*['"][A-Z][A-Za-z]+['"]""", source_code):
        return strategy_key_value_pair("name")

    # Heuristic 2: { modelName: 'SomeModel' }
    if re.search(r"""[{,]\s*modelName\s*:\s*['"][A-Z][A-Za-z]+['"]""", source_code):
        return strategy_key_value_pair("modelName")

    # Heuristic 3: const MODELS = ['User', 'Product', ...]
    m = re.search(
        r"""const\s+([A-Z_]+)\s*=\s*\[\s*'[A-Z][A-Za-z]+'""",
        source_code
    )
    if m:
        return strategy_array_of_strings(m.group(1))

    return None


def _join_paths(prefix, path):
    joined = prefix.rstrip("/") + "/" + path.lstrip("/")
    return joined.rstrip("/") or "/"


def express_apis(
    repo_dir,
    resource_generators=None,
    resource_list_methods=None,
    resource_detail_methods=None,
    model_name_extractor=None,
    extra_skip_dirs=None,
):
    """
    JS API extractor for Express/Fastify/Koa style APIs.

    Parameters
    ----------
    repo_dir : str
        Root directory of the repository to scan.

    resource_generators : set[str] | None
        Lower-cased variable names that call .resource() to auto-generate
        CRUD endpoints (e.g. finale, epilogue).
        Defaults to DEFAULT_RESOURCE_GENERATORS.

    resource_list_methods : tuple[str] | None
        HTTP methods generated on list (non-parameterised) endpoints.
        Defaults to DEFAULT_RESOURCE_LIST_METHODS.

    resource_detail_methods : tuple[str] | None
        HTTP methods generated on detail (parameterised) endpoints.
        Defaults to DEFAULT_RESOURCE_DETAIL_METHODS.

    model_name_extractor : callable | None
        A function (source_code: str, root_node) -> List[str] that returns
        model names used to resolve template literals in .resource() calls.

        Pass one of the built-in strategies:
            strategy_key_value_pair('name')        # { name: 'User', ... }
            strategy_key_value_pair('modelName')
            strategy_array_of_strings('MODELS')    # const MODELS = ['User']
            strategy_regex(r"model\\('(\\w+)'\\)")

        Or write a custom function:
            def my_extractor(source_code, root_node):
                return ['User', 'Product']   # hard-coded or any logic

        If None (default), the extractor is auto-detected per-file by
        scanning for known patterns. If nothing is recognised the file
        produces no model names (template literals resolve to nothing).

    extra_skip_dirs : set[str] | None
        Additional directory name fragments to skip (case-insensitive).
        Merged with the built-in SKIP_DIRS {'test', 'node_modules'}.
        Example: {'codefixes', 'frontend', 'fixtures'}

    Returns
    -------
    list[dict]  — deduplicated endpoint dictionaries.

    Examples
    --------
    # Juice-shop (auto-detect works fine):
    js_apis('./owasp_repo')

    # Juice-shop (explicit):
    js_apis(
        './owasp_repo',
        model_name_extractor=strategy_key_value_pair('name'),
        extra_skip_dirs={'codefixes', 'frontend'},
    )

    # Custom repo with its own ORM:
    js_apis(
        './my_repo',
        resource_generators={'myorm'},
        model_name_extractor=strategy_regex(r"db\\.model\\('(\\w+)'"),
    )
    """
    # Apply defaults
    rgen     = {g.lower() for g in resource_generators} if resource_generators \
               else DEFAULT_RESOURCE_GENERATORS
    rl_meth  = resource_list_methods   or DEFAULT_RESOURCE_LIST_METHODS
    rd_meth  = resource_detail_methods or DEFAULT_RESOURCE_DETAIL_METHODS
    skip_dirs = SKIP_DIRS | ({s.lower() for s in extra_skip_dirs} if extra_skip_dirs else set())

    parser = get_parser('javascript')

    def read_file(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def resolve_require_path(current_file, require_path):
        if not require_path.startswith("."):
            return None
        base_dir = os.path.dirname(current_file)
        resolved_base = os.path.normpath(os.path.join(base_dir, require_path))
        candidates = [
            resolved_base,
            resolved_base + ".js",
            resolved_base + ".ts",
            os.path.join(resolved_base, "index.js"),
            os.path.join(resolved_base, "index.ts"),
        ]
        for candidate in candidates:
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)
        return None

    def extract_string_value(source_code, node):
        return source_code[node.start_byte:node.end_byte].strip("\"'`")

    def extract_file_info(file_path):
        try:
            source_code = read_file(file_path)
        except Exception:
            return [], [], {}

        tree = parser.parse(bytes(source_code, "utf8"))
        root = tree.root_node

        # Determine which model name extractor to use for this file
        if model_name_extractor is not None:
            active_extractor = model_name_extractor
        else:
            active_extractor = _autodetect_strategy(source_code)

        model_names = active_extractor(source_code, root) if active_extractor else []

        endpoints = []
        mounts    = []
        requires  = {}

        def get_args(args_node):
            return [c for c in args_node.children if c.type not in ("(", ")", ",")]

        def try_extract_require(node):
            if node.type != "call_expression":
                return None
            fn   = node.child_by_field_name("function")
            args = node.child_by_field_name("arguments")
            if fn and source_code[fn.start_byte:fn.end_byte] == "require" and args:
                children = get_args(args)
                if children and children[0].type == "string":
                    return extract_string_value(source_code, children[0])
            return None

        def parse_template_string(node):
            parts = []
            for child in node.children:
                if child.type == "string_fragment":
                    text = source_code[child.start_byte:child.end_byte]
                    if text:
                        parts.append(("literal", text))
                elif child.type == "template_substitution":
                    for sub in child.children:
                        if sub.type == "identifier":
                            parts.append(("var", source_code[sub.start_byte:sub.end_byte]))
            return parts

        def resolve_template(parts, name_value):
            return "".join(name_value if kind == "var" else text for kind, text in parts)

        def extract_endpoints_from_object(obj_node):
            resolved_paths = []
            for child in obj_node.children:
                if child.type != "pair":
                    continue
                k = child.child_by_field_name("key")
                v = child.child_by_field_name("value")
                if not (k and v):
                    continue
                if source_code[k.start_byte:k.end_byte].strip("\"'`") != "endpoints":
                    continue
                if v.type != "array":
                    continue
                for item in v.children:
                    if item.type == "string":
                        resolved_paths.append(extract_string_value(source_code, item))
                    elif item.type == "template_string":
                        parts = parse_template_string(item)
                        has_vars = any(kind == "var" for kind, _ in parts)
                        if not has_vars:
                            resolved_paths.append("".join(t for _, t in parts))
                        else:
                            for name_val in model_names:
                                path = resolve_template(parts, name_val)
                                if path.startswith("/"):
                                    resolved_paths.append(path)
            return resolved_paths

        def traverse(node):
            # 1. require() declarations
            if node.type == "variable_declarator":
                name_node  = node.child_by_field_name("name")
                value_node = node.child_by_field_name("value")
                if name_node and value_node:
                    var_name = source_code[name_node.start_byte:name_node.end_byte].strip()
                    req_path = try_extract_require(value_node)
                    if req_path:
                        resolved = resolve_require_path(file_path, req_path)
                        if resolved:
                            requires[var_name] = resolved

            # 2. ES6 default imports
            elif node.type == "import_statement":
                source_str   = None
                default_name = None
                for child in node.children:
                    if child.type == "string":
                        source_str = extract_string_value(source_code, child)
                    elif child.type == "import_clause":
                        for sub in child.children:
                            if sub.type == "identifier":
                                default_name = source_code[sub.start_byte:sub.end_byte].strip()
                if source_str and default_name:
                    resolved = resolve_require_path(file_path, source_str)
                    if resolved:
                        requires[default_name] = resolved

            # 3. Call expressions
            elif node.type == "call_expression":
                func_node = node.child_by_field_name("function")
                args_node = node.child_by_field_name("arguments")

                if func_node and func_node.type == "member_expression" and args_node:
                    method_node = func_node.child_by_field_name("property")
                    obj_node    = func_node.child_by_field_name("object")

                    if method_node and obj_node:
                        method = source_code[method_node.start_byte:method_node.end_byte].lower()
                        obj    = source_code[obj_node.start_byte:obj_node.end_byte].strip()
                        args   = get_args(args_node)

                        # 3a. Router mounting
                        if method == "use" and len(args) >= 2:
                            prefix_node = args[0]
                            router_arg  = args[1]
                            if prefix_node.type == "string":
                                prefix = extract_string_value(source_code, prefix_node)
                                if prefix.startswith("/"):
                                    router_var  = None
                                    router_file = None
                                    if router_arg.type == "identifier":
                                        router_var = source_code[
                                            router_arg.start_byte:router_arg.end_byte
                                        ].strip()
                                    elif router_arg.type == "call_expression":
                                        req_path = try_extract_require(router_arg)
                                        if req_path:
                                            router_file = resolve_require_path(file_path, req_path)
                                        else:
                                            callee = router_arg.child_by_field_name("function")
                                            if callee and callee.type == "identifier":
                                                router_var = source_code[
                                                    callee.start_byte:callee.end_byte
                                                ].strip()
                                    if router_var or router_file:
                                        mounts.append({
                                            "prefix":      prefix,
                                            "router_var":  router_var,
                                            "router_file": router_file,
                                        })

                        # 3b. Auto-generated CRUD  e.g. finale.resource({...})
                        elif method == "resource" and obj.lower() in rgen:
                            if len(args) >= 1 and args[0].type == "object":
                                ep_paths  = extract_endpoints_from_object(args[0])
                                code_text = "\n".join(source_code[node.start_byte:node.end_byte].splitlines()[:30])
                                for ep_path in ep_paths:
                                    if not ep_path.startswith("/"):
                                        continue
                                    dynamic = ":" in ep_path or "*" in ep_path
                                    methods = rd_meth if dynamic else rl_meth
                                    for m in methods:
                                        endpoints.append({
                                            "file":          file_path,
                                            "method":        m,
                                            "path":          ep_path,
                                            "dynamic":       dynamic,
                                            "dynamic_value": ep_path if dynamic else None,
                                            "code":          code_text,
                                            "_obj":          obj,
                                        })

                        # 3c. Route registration  e.g. router.get('/path', handler)
                        elif method in HTTP_METHODS and len(args) >= 1:
                            path_node = args[0]
                            if path_node.type == "string":
                                path_text = extract_string_value(source_code, path_node)
                                if (
                                    path_text.startswith("/")
                                    and "+" not in path_text
                                    and not path_text.lower().endswith(STATIC_EXTENSIONS)
                                    and not any(x in path_text for x in ["{", "}", "\\"])
                                ):
                                    dynamic = ":" in path_text or "*" in path_text
                                    endpoints.append({
                                        "file":          file_path,
                                        "method":        method.upper(),
                                        "path":          path_text,
                                        "dynamic":       dynamic,
                                        "dynamic_value": path_text if dynamic else None,
                                        "code":          "\n".join(source_code[node.start_byte:node.end_byte].splitlines()[:30]),
                                        "_obj":          obj,
                                    })

            for child in node.children:
                traverse(child)

        traverse(root)
        return endpoints, mounts, requires


    js_files = []
    for root_dir, _, files in os.walk(repo_dir):
        if any(skip in root_dir.lower() for skip in skip_dirs):
            continue
        js_files.extend(
            os.path.abspath(os.path.join(root_dir, f))
            for f in files
            if f.endswith((".js", ".ts"))
        )

    file_info = {}
    for file in js_files:
        endpoints, mounts, requires = extract_file_info(file)
        file_info[file] = {"endpoints": endpoints, "mounts": mounts, "requires": requires}


    file_prefixes = {f: [] for f in file_info}

    def resolve_mount_target(mount, local_requires):
        if mount["router_file"] and mount["router_file"] in file_prefixes:
            return mount["router_file"]
        var = mount["router_var"]
        if var and var in local_requires:
            resolved = local_requires[var]
            if resolved in file_prefixes:
                return resolved
        return None

    for file, info in file_info.items():
        for mount in info["mounts"]:
            target = resolve_mount_target(mount, info["requires"])
            if target:
                file_prefixes[target].append(mount["prefix"])

    changed = True
    while changed:
        changed = False
        for file, info in file_info.items():
            for mount in info["mounts"]:
                target = resolve_mount_target(mount, info["requires"])
                if not target:
                    continue
                for parent_prefix in file_prefixes.get(file, []):
                    new_prefix = _join_paths(parent_prefix, mount["prefix"])
                    if new_prefix not in file_prefixes[target]:
                        file_prefixes[target].append(new_prefix)
                        changed = True

    same_file_var_prefix = {}
    for file, info in file_info.items():
        var_map = {}
        for mount in info["mounts"]:
            if mount["router_var"] and mount["router_var"] not in info["requires"]:
                var_map[mount["router_var"]] = mount["prefix"]
        same_file_var_prefix[file] = var_map


    all_endpoints = []
    for file, info in file_info.items():
        cross_file_prefixes = file_prefixes.get(file, [])
        var_prefix_map      = same_file_var_prefix.get(file, {})

        for ep in info["endpoints"]:
            local_prefix = var_prefix_map.get(ep["_obj"])
            base_path    = _join_paths(local_prefix, ep["path"]) if local_prefix else ep["path"]

            targets = [_join_paths(cp, base_path) for cp in cross_file_prefixes] or [base_path]
            for full_path in targets:
                final = {k: v for k, v in ep.items() if not k.startswith("_")}
                final["path"]          = full_path
                final["dynamic"]       = ":" in full_path or "*" in full_path
                final["dynamic_value"] = full_path if final["dynamic"] else None
                all_endpoints.append(final)

    # Deduplicate
    unique = {}
    for e in all_endpoints:
        key = (e["method"], e["path"])
        if key not in unique:
            unique[key] = e

    return list(unique.values())