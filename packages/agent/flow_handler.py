"""Data-only flow handlers; no imports, host objects, I/O or unrestricted Python execution.

The accepted language is one run(input, context) function using JSON values,
assignments, conditions, arithmetic and selected pure builtins. It is deliberately
smaller than Python, rather than relying on a temporary cwd as a security boundary.
"""
import ast
import json

BUILTINS = {name: value for name, value in {
    'abs': abs, 'bool': bool, 'dict': dict, 'float': float, 'int': int,
    'len': len, 'list': list, 'max': max, 'min': min, 'round': round,
    'sorted': sorted, 'str': str, 'sum': sum, 'tuple': tuple,
}.items()}
NODES = (
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign,
    ast.If, ast.Expr, ast.Name, ast.Load, ast.Store, ast.Constant, ast.Dict,
    ast.List, ast.Tuple, ast.Subscript, ast.Slice, ast.IfExp, ast.BoolOp,
    ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd, ast.BinOp,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Compare,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.Is, ast.IsNot, ast.Call, ast.keyword, ast.Attribute,
)


def dictionary_get(value, key, default=None):
    if type(value) is not dict:
        raise ValueError('流程 .get 只支持 JSON 对象')
    return value.get(key, default)


class GetCalls(ast.NodeTransformer):
    def visit_Call(self, node):
        self.generic_visit(node)
        if isinstance(node.func, ast.Attribute):
            return ast.copy_location(ast.Call(func=ast.Name(id='_dictionary_get', ctx=ast.Load()),
                                             args=[node.func.value, *node.args], keywords=node.keywords), node)
        return node


def compile_handler(source):
    if not isinstance(source, str) or not source.strip() or len(source) > 20000:
        raise ValueError('流程代码须为不超过 20000 字的纯数据转换')
    try:
        tree = ast.parse(source, mode='exec')
    except SyntaxError as error:
        raise ValueError('流程代码语法无效') from error
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise ValueError('流程代码只允许一个 run(input, context) 函数')
    function = tree.body[0]
    args = function.args
    if (function.name != 'run' or function.decorator_list or function.returns or
            args.posonlyargs or len(args.args) != 2 or args.vararg or args.kwarg or
            args.kwonlyargs or args.defaults or args.kw_defaults or
            any(arg.annotation or arg.arg.startswith('_') or arg.arg in BUILTINS or arg.arg == 'run' for arg in args.args)):
        raise ValueError('流程 run 只接收两个普通参数，不支持装饰器或注解')
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    for node in ast.walk(tree):
        if not isinstance(node, NODES) or (isinstance(node, ast.FunctionDef) and node is not function):
            raise ValueError('流程代码只支持 JSON 数据转换，禁止导入、循环与主机操作')
        if isinstance(node, ast.Name):
            if node.id.startswith('_') or node.id == 'run' or (isinstance(node.ctx, ast.Store) and node.id in BUILTINS):
                raise ValueError('流程代码不能访问内部名称或替换内置函数')
        if isinstance(node, ast.Expr) and not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            raise ValueError('流程代码不能包含独立执行语句')
        if isinstance(node, ast.Attribute):
            parent = parents[node]
            if node.attr != 'get' or not isinstance(parent, ast.Call) or parent.func is not node:
                raise ValueError('流程代码只允许 JSON 对象的 .get 方法')
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id not in BUILTINS:
                    raise ValueError('流程代码只能调用允许的数据处理函数')
            elif not isinstance(node.func, ast.Attribute):
                raise ValueError('流程代码不能调用动态对象')
            if any(keyword.arg is None for keyword in node.keywords):
                raise ValueError('流程代码不支持动态关键字参数')
    tree = ast.fix_missing_locations(GetCalls().visit(tree))
    return compile(tree, '<flow-handler>', 'exec')


def run_handler(source, inputs, context):
    code = compile_handler(source)  # Recheck persisted handlers, including legacy versions.
    inputs, context = json.loads(json.dumps([inputs, context], allow_nan=False))
    namespace = {'__builtins__': BUILTINS, '_dictionary_get': dictionary_get}
    exec(code, namespace)
    result = namespace['run'](inputs, context)
    if not isinstance(result, dict):
        raise ValueError('执行代码必须返回对象')
    json.dumps(result, allow_nan=False)
    return result
