import os
import ast
from tree_sitter import Language, Parser, Query, QueryCursor
import tree_sitter_python as tspy

def fastapi_apis(repo_dir):
    """
    Static FastAPI API extractor for LLM tool usage.
    Returns a list of endpoint dictionaries:
    [
        {
            "method": "GET",
            "path": "/users/{id}",
            "dynamic": True/False,
            "dynamic_value": None or expression,
            "code": "...full function code..."
        },
        ...
    ]
    """

    PY_LANGUAGE = Language(tspy.language())
    parser = Parser(PY_LANGUAGE)

    HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}

    DECORATED_FUNC_QUERY = "(decorated_definition) @endpoint"

    ROUTER_ASSIGNMENT_QUERY = """
    (assignment
        left: (identifier) @router_var
        right: (call
            function: (identifier) @apirouter_call
        )
    )
    """

    INCLUDE_ROUTER_QUERY = """
    (call
        function: (attribute
            object: (identifier) @app_obj
            attribute: (identifier) @include
        )
        arguments: (argument_list) @args
    )
    """

    def read_file(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def find_child_by_type(node, type_name):
        """Return first direct child matching the given type."""
        for child in node.children:
            if child.type == type_name:
                return child
        return None

    def get_capture_nodes(captures, name):
        if isinstance(captures, dict):
            return captures.get(name, [])
        return [node for node, cap_name in captures if cap_name == name]

    
    def get_decorator_info(decorator_node, source_code):
        """
        Extract HTTP method, path, dynamic flag, and router object from a decorator node.
        - Normalizes trailing slashes
        - Marks dynamic=True if path contains `{...}`
        """
        def find_child_by_type(node, type_name):
            for child in node.children:
                if child.type == type_name:
                    return child
            return None

        call_node = find_child_by_type(decorator_node, "call")
        if not call_node:
            return None, None, None, None, None

        func_node = call_node.child_by_field_name("function")
        if not func_node or func_node.type != "attribute":
            return None, None, None, None, None

        attr_node = func_node.child_by_field_name("attribute")
        obj_node = func_node.child_by_field_name("object")
        if not attr_node:
            return None, None, None, None, None

        # HTTP method (GET, POST, etc.)
        method = source_code[attr_node.start_byte:attr_node.end_byte].upper()
        obj_name = source_code[obj_node.start_byte:obj_node.end_byte].strip() if obj_node else ""

        # Default values
        path = "/"
        dynamic = False
        dynamic_value = None

        args_node = call_node.child_by_field_name("arguments")
        if args_node:
            # Skip punctuation and get the first "real" argument
            first_arg = None
            for child in args_node.children:
                if child.type not in ("(", ")", ","):
                    first_arg = child
                    break

            if first_arg:
                if first_arg.type in ("string", "concatenated_string"):
                    path = ast.literal_eval(source_code[first_arg.start_byte:first_arg.end_byte])
                    # Normalize trailing slash (keep root "/")
                    path = path.rstrip("/") if path != "/" else path
                    # Check for dynamic path parameters like {user_id}
                    if "{" in path and "}" in path:
                        dynamic = True
                        dynamic_value = path
                    else:
                        dynamic = False
                        dynamic_value = None
                else:
                    # Non-literal path argument
                    path = source_code[first_arg.start_byte:first_arg.end_byte].strip()
                    dynamic = True
                    dynamic_value = path

        return method, path, dynamic, dynamic_value, obj_name

    def extract_routers_and_prefixes(file_path):
        source_code = read_file(file_path)
        tree = parser.parse(bytes(source_code, "utf8"))

        # Collect all APIRouter variable names
        query_router = QueryCursor(Query(PY_LANGUAGE, ROUTER_ASSIGNMENT_QUERY))
        captures = query_router.captures(tree.root_node)
        routers = set()
        for node in get_capture_nodes(captures, "router_var"):
            routers.add(source_code[node.start_byte:node.end_byte].strip())

        # Collect include_router calls and their prefix arguments
        query_include = QueryCursor(Query(PY_LANGUAGE, INCLUDE_ROUTER_QUERY))
        captures2 = query_include.captures(tree.root_node)

        router_prefix_map = {}
        include_nodes = get_capture_nodes(captures2, "include")
        args_nodes = get_capture_nodes(captures2, "args")

        for i, inc_node in enumerate(include_nodes):
            inc_name = source_code[inc_node.start_byte:inc_node.end_byte]
            if inc_name != "include_router" or i >= len(args_nodes):
                continue

            arg_text = source_code[args_nodes[i].start_byte:args_nodes[i].end_byte].strip("()")
            parts = arg_text.split(",")
            if not parts:
                continue

            router_var = parts[0].strip()
            prefix = None
            for p in parts[1:]:
                if "prefix" in p and "=" in p:
                    prefix_val = p.split("=", 1)[1].strip()
                    try:
                        prefix = ast.literal_eval(prefix_val)
                    except Exception:
                        prefix = prefix_val.strip("\"'")
            if router_var:
                router_prefix_map[router_var] = prefix

        return routers, router_prefix_map

    def extract_endpoints_from_file(file_path, router_prefix_map=None):
        if router_prefix_map is None:
            router_prefix_map = {}

        source_code = read_file(file_path)
        tree = parser.parse(bytes(source_code, "utf8"))

        query = QueryCursor(Query(PY_LANGUAGE, DECORATED_FUNC_QUERY))
        captures = query.captures(tree.root_node)
        endpoint_nodes = get_capture_nodes(captures, "endpoint")

        endpoints = []
        for decorated_node in endpoint_nodes:
            # Collect all decorator children of this decorated_definition
            decorators = [child for child in decorated_node.children if child.type == "decorator"]

            func_node = decorated_node.child_by_field_name("definition")
            if func_node is None:
                func_node = find_child_by_type(decorated_node, "function_definition")
            if func_node is None:
                continue

            func_code = source_code[func_node.start_byte:func_node.end_byte]

            for decorator in decorators:
                result = get_decorator_info(decorator, source_code)
                method, path, dynamic, dynamic_value, obj_name = result

                if method is None:
                    continue

                # Only emit endpoints for HTTP method decorators
                if method.lower() not in HTTP_METHODS:
                    continue

                prefix = router_prefix_map.get(obj_name) or ""
                if prefix:
                    if dynamic:
                        dynamic_value = f"{prefix} + {dynamic_value or path}"
                    path = prefix + path

                endpoints.append({
                    "method": method,
                    "path": path,
                    "dynamic": dynamic,
                    "dynamic_value": dynamic_value,
                    "code": func_code,
                })

        return endpoints

    # Gather all Python files
    py_files = []
    for root, dirs, files in os.walk(repo_dir):
        py_files.extend([os.path.join(root, f) for f in files if f.endswith(".py")])

    # First pass: collect router variable names and their prefixes
    router_prefix_map = {}
    for file in py_files:
        _, prefixes = extract_routers_and_prefixes(file)
        router_prefix_map.update(prefixes)

    # Second pass: extract endpoints using the resolved prefix map
    all_endpoints = []
    for file in py_files:
        endpoints = extract_endpoints_from_file(file, router_prefix_map)
        all_endpoints.extend(endpoints)

    return all_endpoints