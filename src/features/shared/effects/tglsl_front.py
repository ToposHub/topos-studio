"""受限 GLSL（.tglsl ``process`` 子集）前端：词法、语法、AST 与结构化诊断。

阶段 6.3「可译 .tglsl」的第一步。转译器（``tglsl_msl_transpiler``）只接受
这里产出的 AST，因此本模块决定「什么能进子集」：

可解析（进入后续类型检查）
    - 全局 ``const`` 标量/向量常量；
    - 插件自定义函数（``in`` / ``const`` 形参限定符按 GLSL 语义丢弃）；
    - 局部声明（含多声明符、可选初始化）、赋值与复合赋值、自增自减；
    - ``if`` / ``else``、``return``、代码块、空语句；
    - 标量/向量/布尔向量字面量、构造器、swizzle、下标、一元/二元/三目、
      函数调用。

显式拒绝（带阶段、代码、行列与源码片段的诊断，绝不静默近似）
    - 循环（``for`` / ``while`` / ``do``）、``switch``、``break`` / ``continue``；
    - 预处理指令（宏、扩展、版本、包含——按指令名报告）；
    - 矩阵、采样器、数组、``struct``、接口限定符与插件自声明的 ``uniform``；
    - ``out`` / ``inout`` 形参、``discard``、非 const 全局变量。

线程边界：纯字符串处理，无状态、无 GPU/Qt 依赖，可在任意线程调用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, NoReturn, Optional, Tuple

#: 转译器版本——进入 Metal 编译缓存键（见 ``metal_abi.MetalCompileKey``）。
TRANSPILER_VERSION = "tglsl-msl/1.0.0"


class TranspileStage(str, Enum):
    """诊断所属阶段（测试诊断要求包含阶段）。"""

    PARSE = "parse"
    VALIDATE = "validate"
    TYPECHECK = "typecheck"
    GENERATE = "generate"
    COMPILE = "compile"
    RUN = "run"


@dataclass(frozen=True)
class TranspileIssue:
    """一条结构化转译诊断（不含插件身份——身份由上层 ``TglslMetalDiagnostic`` 补齐）。"""

    stage: str
    code: str
    message: str
    line: int = -1
    column: int = -1
    snippet: str = ""

    def format(self) -> str:
        """人类可读单行（阶段 + 代码 + 位置 + 消息 + 片段）。"""
        where = f"{self.line}:{self.column}" if self.line > 0 else "no-location"
        text = f"[{self.stage}] {self.code} ({where}): {self.message}"
        if self.snippet:
            text += f" | `{self.snippet}`"
        return text


class TglslFrontError(Exception):
    """前端拒绝：携带结构化 :class:`TranspileIssue`（不退化为裸字符串）。"""

    def __init__(self, issue: TranspileIssue) -> None:
        super().__init__(issue.format())
        self.issue = issue


BACKEND_METAL = "metal"


@dataclass(frozen=True)
class TglslMetalDiagnostic:
    """.tglsl → Metal 链路上的一条诊断：插件 ID、版本、阶段、后端缺一不可。

    计划阶段 6 的要求：「测试诊断包含插件 ID、版本、阶段、后端与精简编译
    消息」。``effect_id`` 同时携带 EffectCatalog 的 ``plugin:<id>`` 形式，
    便于把回退原因定位到具体效果。
    """

    plugin_id: str
    plugin_version: str
    stage: str
    backend: str = BACKEND_METAL
    code: str = ""
    message: str = ""
    line: int = -1
    column: int = -1
    snippet: str = ""

    @classmethod
    def from_issue(cls, issue: TranspileIssue, *, plugin_id: str,
                   plugin_version: str, backend: str = BACKEND_METAL) -> "TglslMetalDiagnostic":
        return cls(plugin_id=plugin_id, plugin_version=plugin_version, stage=issue.stage,
                   backend=backend, code=issue.code, message=issue.message,
                   line=issue.line, column=issue.column, snippet=issue.snippet)

    @property
    def effect_id(self) -> str:
        return self.plugin_id if self.plugin_id.startswith("plugin:") else f"plugin:{self.plugin_id}"

    def format(self) -> str:
        where = f" {self.line}:{self.column}" if self.line > 0 else ""
        text = (f"plugin={self.plugin_id or '<unknown>'} version={self.plugin_version or '<none>'}"
                f" stage={self.stage} backend={self.backend} code={self.code}{where}:"
                f" {self.message}")
        if self.snippet:
            text += f" | `{self.snippet}`"
        return text

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plugin_id": self.plugin_id, "plugin_version": self.plugin_version,
            "stage": self.stage, "backend": self.backend, "code": self.code,
            "message": self.message, "line": self.line, "column": self.column,
            "snippet": self.snippet,
        }


class TglslMetalError(Exception):
    """转译/编译/运行拒绝：携带 :class:`TglslMetalDiagnostic`。"""

    def __init__(self, diagnostic: TglslMetalDiagnostic) -> None:
        super().__init__(diagnostic.format())
        self.diagnostic = diagnostic


# ── 类型 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GlslType:
    """GLSL 标量/向量类型：基类型 + 分量数（1 = 标量）。矩阵不在子集内。"""

    base: str          # "float" | "int" | "uint" | "bool"
    size: int          # 1..4

    @property
    def name(self) -> str:
        if self.size == 1:
            return self.base
        prefix = {"float": "vec", "int": "ivec", "uint": "uvec", "bool": "bvec"}
        return f"{prefix[self.base]}{self.size}"

    @property
    def is_float(self) -> bool:
        return self.base == "float"

    @property
    def is_bool(self) -> bool:
        return self.base == "bool"

    @property
    def is_scalar(self) -> bool:
        return self.size == 1

    def at_size(self, size: int) -> "GlslType":
        return GlslType(self.base, size)


FLOAT = GlslType("float", 1)
INT = GlslType("int", 1)
UINT = GlslType("uint", 1)
BOOL = GlslType("bool", 1)

GLSL_TYPES: Dict[str, GlslType] = {"float": FLOAT, "int": INT, "uint": UINT, "bool": BOOL}
for _base, _prefix in (("float", "vec"), ("int", "ivec"), ("uint", "uvec"), ("bool", "bvec")):
    for _n in (2, 3, 4):
        GLSL_TYPES[f"{_prefix}{_n}"] = GlslType(_base, _n)

del _base, _prefix, _n

#: ``void`` 只在类型位置合法（函数返回值 / 空形参表）；``FunctionDef.ret`` 用 None 表示。
VOID_TYPE = GlslType("void", 1)

MATRIX_TYPE_NAMES = frozenset({
    "mat2", "mat3", "mat4", "mat2x2", "mat2x3", "mat2x4", "mat3x2", "mat3x3", "mat3x4",
    "mat4x2", "mat4x3", "mat4x4", "dmat2", "dmat3", "dmat4",
})

_PRECISION_QUALIFIERS = frozenset({"highp", "mediump", "lowp"})

# 子集外的关键字 → (诊断代码, 中文说明)。解析器遇到即给出对应诊断。
# 注：``smooth`` 是 GLSL 3.30 的插值关键字，但仓库已发布插件把它用作参数名
#（Frequency_Split）。GL 路径先把裸参数名改写成 ``u_smooth`` 再编译，GLSL
# 编译器看不到这个关键字；Metal 路径同理把它映射成 ``params.p_smooth``。
# 因此这里按普通标识符处理，不做关键字拒绝。
_UNSUPPORTED_KEYWORDS: Dict[str, Tuple[str, str]] = {
    "for": ("unsupported_construct:loop", "for 循环不在可译子集内（子集 = 纯像素算子 + 受控采样）"),
    "while": ("unsupported_construct:loop", "while 循环不在可译子集内"),
    "do": ("unsupported_construct:loop", "do 循环不在可译子集内"),
    "switch": ("unsupported_construct:switch", "switch 语句不在可译子集内"),
    "case": ("unsupported_construct:switch", "case 标签不在可译子集内"),
    "default": ("unsupported_construct:switch", "default 标签不在可译子集内"),
    "break": ("unsupported_construct:break", "break 只出现在循环/switch 中，二者均不在可译子集内"),
    "continue": ("unsupported_construct:continue", "continue 只出现在循环中，循环不在可译子集内"),
    "discard": ("unsupported_construct:discard", "discard（丢弃片元）在 compute 转译中没有等价语义"),
    "struct": ("unsupported_construct:struct", "struct 类型不在可译子集内"),
    "uniform": ("unsupported_construct:uniform", "插件不得自行声明 uniform；参数只能经 @param 声明"),
    "layout": ("unsupported_construct:interface", "layout 接口限定符不在可译子集内"),
    "attribute": ("unsupported_construct:interface", "attribute 接口限定符不在可译子集内"),
    "varying": ("unsupported_construct:interface", "varying 接口限定符不在可译子集内"),
    "flat": ("unsupported_construct:interface", "flat 插值限定符不在可译子集内"),
    "noperspective": ("unsupported_construct:interface", "noperspective 插值限定符不在可译子集内"),
    "centroid": ("unsupported_construct:interface", "centroid 插值限定符不在可译子集内"),
    "invariant": ("unsupported_construct:interface", "invariant 限定符不在可译子集内"),
}

# 采样器类型名（GLSL 1D/2D/3D/Cube 及其 Array/Rect/Shadow/MS 变体，含 i/u 前缀）。
# 必须整词匹配：`material` / `sampler_gain` 这类合法标识符不得被误判。
_SAMPLER_TYPE_RE = re.compile(r"^(?:[iu])?sampler(?:1D|2D|3D|Cube)(?:Array|Rect|Shadow|MS|MSArray|Buffer)?$")


def _is_matrix_name(name: str) -> bool:
    return name in MATRIX_TYPE_NAMES


def _is_sampler_name(name: str) -> bool:
    return _SAMPLER_TYPE_RE.match(name) is not None

_PREPROC_NAMES: Dict[str, str] = {
    "define": "宏定义", "undef": "宏取消", "include": "文件包含", "version": "版本声明",
    "extension": "扩展启用", "if": "条件编译", "ifdef": "条件编译", "ifndef": "条件编译",
    "elif": "条件编译", "else": "条件编译", "endif": "条件编译", "error": "错误指令",
    "pragma": "pragma 指令", "line": "行指令",
}


# ── AST ─────────────────────────────────────────────────────────────────


@dataclass
class Node:
    line: int = 0
    col: int = 0


@dataclass
class Expr(Node):
    """表达式基类。``ty`` / ``ref`` 由转译器的类型检查阶段回填。"""

    ty: Optional[GlslType] = field(default=None, repr=False)
    ref: Any = field(default=None, repr=False)


@dataclass
class Literal(Expr):
    text: str = ""


@dataclass
class Ident(Expr):
    name: str = ""


@dataclass
class Constructor(Expr):
    """类型构造器；``Expr.ty`` 在这里表示**目标类型**。"""

    args: Tuple[Expr, ...] = ()


@dataclass
class Swizzle(Expr):
    base: Optional[Expr] = None
    components: str = ""               # 原文分量名（rgb / xyz / stpq）


@dataclass
class Index(Expr):
    base: Optional[Expr] = None
    index: Optional[Expr] = None


@dataclass
class Unary(Expr):
    op: str = ""                       # - + ! ~ ++ -- post++ post--
    operand: Optional[Expr] = None


@dataclass
class Binary(Expr):
    op: str = ""
    left: Optional[Expr] = None
    right: Optional[Expr] = None


@dataclass
class Ternary(Expr):
    cond: Optional[Expr] = None
    then: Optional[Expr] = None
    els: Optional[Expr] = None


@dataclass
class Call(Expr):
    callee: str = ""
    args: Tuple[Expr, ...] = ()


@dataclass
class Stmt(Node):
    pass


@dataclass
class VarDecl(Stmt):
    ty: Optional[GlslType] = None
    name: str = ""
    init: Optional[Expr] = None
    const: bool = False


@dataclass
class Assign(Stmt):
    op: str = ""                       # = += -= *= /= %= <<= >>= &= |= ^=
    target: Optional[Expr] = None
    value: Optional[Expr] = None


@dataclass
class IncDec(Stmt):
    op: str = ""                       # ++ / --
    target: Optional[Expr] = None
    prefix: bool = False


@dataclass
class ExprStmt(Stmt):
    expr: Optional[Expr] = None


@dataclass
class If(Stmt):
    cond: Optional[Expr] = None
    then: Optional[Stmt] = None
    els: Optional[Stmt] = None


@dataclass
class Return(Stmt):
    value: Optional[Expr] = None


@dataclass
class Block(Stmt):
    stmts: Tuple[Stmt, ...] = ()


@dataclass
class Empty(Stmt):
    pass


@dataclass
class Param:
    line: int = 0
    col: int = 0
    ty: Optional[GlslType] = None
    name: str = ""


@dataclass
class FunctionDef(Node):
    ret: Optional[GlslType] = None     # None = void
    name: str = ""
    params: Tuple[Param, ...] = ()
    body: Optional[Block] = None


@dataclass
class GlobalConst(Node):
    ty: Optional[GlslType] = None
    name: str = ""
    init: Optional[Expr] = None


@dataclass
class TranslationUnit:
    """一个 ``.tglsl`` 元数据块之后的全部 GLSL。"""

    source: str = ""
    consts: Tuple[GlobalConst, ...] = ()
    functions: Tuple[FunctionDef, ...] = ()


def parse_tglsl_glsl(source: str) -> TranslationUnit:
    """把 ``.tglsl`` 的着色器部分解析为 :class:`TranslationUnit`。

    只做词法/语法与「子集外结构」的识别；入口、保留字与类型检查属于转译器。
    无法解析时抛 :class:`TglslFrontError`（含阶段、代码、行列、片段）。
    """
    return _Parser(_lex(source), source).parse_unit()


# ── 词法 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Token:
    kind: str      # IDENT FLOAT INT UINT OP PREPROC EOF
    text: str
    line: int
    col: int


# 顺序即优先级：FLOAT 先于 UINT/INT；多字符运算符先于单字符。
_TOKEN_RE = re.compile(
    r"(?P<FLOAT>(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+)"
    r"|(?P<UINT>(?:0[xX][0-9a-fA-F]+|\d+)[uU])"
    r"|(?P<INT>0[xX][0-9a-fA-F]+|\d+)"
    r"|(?P<IDENT>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<OP><<=?|>>=?|&&|\|\||\^\^|<=|>=|==|!=|\+\+|--|[+\-*/%&|^]="
    r"|[-+*/%<>=!&|^~?:;,.(){}\[\]])"
)
_BLOCK_COMMENT_RE = re.compile(r"/\*(?:[^*]|\*(?!/))*\*/", re.DOTALL)

_ASSIGN_OPS = frozenset({"=", "+=", "-=", "*=", "/=", "%=", "<<=", ">>=", "&=", "|=", "^="})


def _line_snippet(source: str, line: int) -> str:
    lines = source.split("\n")
    if 1 <= line <= len(lines):
        return lines[line - 1].strip()[:80]
    return ""


def _lex(source: str) -> List[Token]:
    """GLSL 子集词法分析。注释丢弃；预处理指令整体成词以便给出明确诊断。"""
    tokens: List[Token] = []
    pos = 0
    line = 1
    line_start = 0
    length = len(source)
    at_line_start = True

    def fail(code: str, message: str) -> None:
        raise TglslFrontError(TranspileIssue(
            TranspileStage.PARSE.value, code, message, line, pos - line_start + 1,
            _line_snippet(source, line)))

    while pos < length:
        ch = source[pos]
        if ch == "\n":
            pos += 1
            line += 1
            line_start = pos
            at_line_start = True
            continue
        if ch in " \t\r\f\v":
            pos += 1
            continue
        if ch == "#":
            if not at_line_start:
                fail("unexpected_char", "行内出现 '#'（只识别位于行首的预处理指令）")
            start = pos
            start_line, start_col = line, pos - line_start + 1
            # 反斜杠续行：多行宏体必须整体吞掉，否则宏体会被当作 GLSL 解析。
            while pos < length and source[pos] != "\n":
                if source[pos] == "\\" and pos + 1 < length and source[pos + 1] == "\n":
                    pos += 2
                    line += 1
                    line_start = pos
                    continue
                pos += 1
            tokens.append(Token("PREPROC", source[start + 1:pos].strip(),
                                start_line, start_col))
            continue
        if source.startswith("//", pos):
            end = source.find("\n", pos)
            pos = length if end < 0 else end
            continue
        if source.startswith("/*", pos):
            match = _BLOCK_COMMENT_RE.match(source, pos)
            if match is None:
                fail("unterminated_comment", "块注释未闭合")
            text = match.group(0)
            if "\n" in text:
                line += text.count("\n")
                line_start = match.start() + text.rfind("\n") + 1
            pos = match.end()
            continue
        match = _TOKEN_RE.match(source, pos)
        if match is None:
            fail("unexpected_char", f"无法识别的字符 {ch!r}")
        kind = match.lastgroup or ""
        tokens.append(Token(kind, match.group(0), line, pos - line_start + 1))
        pos = match.end()
        at_line_start = False
    tokens.append(Token("EOF", "", line, pos - line_start + 1))
    return tokens


# ── 语法 ────────────────────────────────────────────────────────────────

# GLSL 二元运算符优先级（**数值越大结合越紧**，与优先级爬升的写法一致）；
# 全部左结合。顺序取自 GLSL 3.30 §5.9：乘除 → 加减 → 移位 → 关系 → 相等 →
# & → ^ → | → && → ^^ → ||。
_BIN_LEVEL: Dict[str, int] = {
    "||": 1,
    "^^": 2,
    "&&": 3,
    "|": 4,
    "^": 5,
    "&": 6,
    "==": 7, "!=": 7,
    "<": 8, ">": 8, "<=": 8, ">=": 8,
    "<<": 9, ">>": 9,
    "+": 10, "-": 10,
    "*": 11, "/": 11, "%": 11,
}

_SWIZZLE_SETS = ("xyzw", "rgba", "stpq")


class _Parser:
    def __init__(self, tokens: List[Token], source: str) -> None:
        self._toks = tokens
        self._src = source
        self._i = 0

    # ── 基础 ──
    def _peek(self, ahead: int = 0) -> Token:
        index = min(self._i + ahead, len(self._toks) - 1)
        return self._toks[index]

    def _next(self) -> Token:
        tok = self._peek()
        if tok.kind != "EOF":
            self._i += 1
        return tok

    def _at(self, kind: str, text: Optional[str] = None) -> bool:
        tok = self._peek()
        return tok.kind == kind and (text is None or tok.text == text)

    def _accept(self, kind: str, text: Optional[str] = None) -> Optional[Token]:
        if self._at(kind, text):
            return self._next()
        return None

    def _expect(self, kind: str, text: Optional[str] = None, what: str = "") -> Token:
        if self._at(kind, text):
            return self._next()
        tok = self._peek()
        if tok.kind == "OP" and tok.text in _ASSIGN_OPS:
            # 表达式里嵌套赋值（如 (b = c)）：子集外，给专门的处理代码而不是含糊的语法错误。
            self._raise("unsupported_construct:assignment_expression",
                        "赋值只能作为语句，不能嵌在表达式里", tok)
        found = "文件结尾" if tok.kind == "EOF" else repr(tok.text)
        self._raise("syntax", f"期望 {what or text or kind}，实际是 {found}", tok)

    def _raise(self, code: str, message: str, tok: Optional[Token] = None) -> NoReturn:
        tok = tok or self._peek()
        raise TglslFrontError(TranspileIssue(
            TranspileStage.PARSE.value, code, message, tok.line, tok.col,
            _line_snippet(self._src, tok.line)))

    def _node(self, cls: type, tok: Token, **kwargs: Any) -> Any:
        return cls(line=tok.line, col=tok.col, **kwargs)

    def _skip_precision_qualifiers(self) -> None:
        while self._at("IDENT") and self._peek().text in _PRECISION_QUALIFIERS:
            self._next()

    def _reject_unsupported_keyword(self, tok: Token) -> None:
        entry = _UNSUPPORTED_KEYWORDS.get(tok.text)
        if entry is not None:
            self._raise(entry[0], f"{entry[1]}（关键字 `{tok.text}`）", tok)

    def _type_at(self) -> Optional[GlslType]:
        """当前记号是子集内类型时返回类型（不消费）；返回 None 表示不是类型。"""
        tok = self._peek()
        if tok.kind != "IDENT":
            return None
        if tok.text in GLSL_TYPES:
            return GLSL_TYPES[tok.text]
        if tok.text == "void":
            return VOID_TYPE
        if _is_matrix_name(tok.text):
            self._raise("unsupported_construct:matrix",
                        f"矩阵类型 `{tok.text}` 不在可译子集内", tok)
        if _is_sampler_name(tok.text):
            self._raise("unsupported_construct:sampler",
                        f"采样器类型 `{tok.text}` 不在可译子集内（只能使用引擎注入的 u_input_tex）",
                        tok)
        return None

    # ── 顶层 ──
    def parse_unit(self) -> TranslationUnit:
        consts: List[GlobalConst] = []
        functions: List[FunctionDef] = []
        while not self._at("EOF"):
            tok = self._peek()
            if tok.kind == "PREPROC":
                name = tok.text.split(None, 1)[0] if tok.text else ""
                label = _PREPROC_NAMES.get(name, f"预处理指令 #{name}")
                self._raise("unsupported_construct:preprocessor",
                            f"{label}不在可译子集内（宏/扩展/版本/包含均需作者展开后才能转译）",
                            tok)
            if tok.kind != "IDENT":
                self._raise("syntax", f"顶层出现意外的记号 {tok.text!r}", tok)
            self._reject_unsupported_keyword(tok)
            if tok.text == "precision":
                # 桌面 GL 上 precision 语句不改变语义：显式允许并忽略。
                while not self._at("OP", ";") and not self._at("EOF"):
                    self._next()
                self._expect("OP", ";", "';'")
                continue
            if tok.text == "in" or tok.text == "out":
                self._raise("unsupported_construct:interface",
                            f"顶层接口限定符 `{tok.text}` 不在可译子集内", tok)
            is_const = self._accept("IDENT", "const") is not None
            self._skip_precision_qualifiers()
            ty = self._type_at()
            if ty is None:
                self._raise("syntax", f"顶层出现意外的标识符 `{tok.text}`", tok)
            self._next()
            name_tok = self._expect("IDENT", what="名称")
            if self._at("OP", "("):
                if is_const:
                    self._raise("unsupported_construct:const_function",
                                "const 不能修饰函数定义", name_tok)
                functions.append(self._function_def(ty, name_tok))
                continue
            if ty is VOID_TYPE:
                self._raise("syntax", "void 类型不能用于变量", name_tok)
            if not is_const:
                self._raise("unsupported_construct:global_mutable",
                            f"全局变量 `{name_tok.text}` 必须是 const（可变全局状态在 compute 转译中没有等价语义）",
                            name_tok)
            while True:
                self._reject_array_declarator()
                eq = self._expect("OP", "=", "'='（全局常量必须有初始化）")
                init = self._expression()
                consts.append(self._node(GlobalConst, name_tok, ty=ty, name=name_tok.text, init=init))
                del eq
                if self._accept("OP", ","):
                    name_tok = self._expect("IDENT", what="常量名")
                    continue
                break
            self._expect("OP", ";", "';'")
        return TranslationUnit(source=self._src, consts=tuple(consts), functions=tuple(functions))

    def _reject_array_declarator(self) -> None:
        if self._at("OP", "["):
            self._raise("unsupported_construct:array",
                        "数组不在可译子集内（常量表请改写为命名常量或受控采样）")

    def _function_def(self, ret: Optional[GlslType], name_tok: Token) -> FunctionDef:
        ret = None if ret is VOID_TYPE else ret
        self._expect("OP", "(", "'('")
        params: List[Param] = []
        if not self._at("OP", ")"):
            if self._at("IDENT", "void") and self._peek(1).kind == "OP" and self._peek(1).text == ")":
                self._next()
            else:
                while True:
                    p_tok = self._peek()
                    if p_tok.kind == "IDENT" and p_tok.text in ("out", "inout"):
                        self._raise("unsupported_construct:out_param",
                                    f"{p_tok.text} 形参 `{self._peek(1).text}` 不在可译子集内"
                                    "（Metal 侧无出参映射）", p_tok)
                    self._accept("IDENT", "const")
                    self._accept("IDENT", "in")
                    self._skip_precision_qualifiers()
                    p_ty = self._type_at()
                    if p_ty is None:
                        self._raise("syntax", "形参缺少类型", self._peek())
                    self._next()
                    p_name = self._accept("IDENT")
                    self._reject_array_declarator()
                    params.append(Param(line=p_tok.line, col=p_tok.col, ty=p_ty,
                                        name=p_name.text if p_name else ""))
                    if self._accept("OP", ","):
                        continue
                    break
        self._expect("OP", ")", "')'")
        body = self._block()
        return self._node(FunctionDef, name_tok, ret=ret, name=name_tok.text,
                          params=tuple(params), body=body)

    # ── 语句 ──
    def _block(self) -> Block:
        open_tok = self._expect("OP", "{", "'{'")
        stmts: List[Stmt] = []
        while not self._at("OP", "}"):
            if self._at("EOF"):
                self._raise("syntax", "代码块未闭合", open_tok)
            stmts.append(self._statement())
        self._expect("OP", "}", "'}'")
        return self._node(Block, open_tok, stmts=tuple(stmts))

    def _statement(self) -> Stmt:
        tok = self._peek()
        if tok.kind == "PREPROC":
            self._raise("unsupported_construct:preprocessor", "函数体内不允许预处理指令", tok)
        if tok.kind == "OP":
            if tok.text == "{":
                return self._block()
            if tok.text == ";":
                self._next()
                return self._node(Empty, tok)
        if tok.kind == "IDENT":
            if tok.text in _UNSUPPORTED_KEYWORDS or _is_matrix_name(tok.text) \
                    or _is_sampler_name(tok.text) or tok.text in ("in", "out", "inout"):
                self._reject_unsupported_keyword(tok)
                if tok.text in ("in", "out", "inout"):
                    self._raise("unsupported_construct:interface",
                                f"语句位置出现限定符 `{tok.text}`", tok)
                self._type_at()          # 矩阵/采样器：抛对应诊断
            if tok.text == "if":
                return self._if_statement()
            if tok.text == "return":
                self._next()
                value = None if self._at("OP", ";") else self._expression()
                self._expect("OP", ";", "';'")
                return self._node(Return, tok, value=value)
            if tok.text == "const" or self._type_at() is not None:
                return self._declaration()
        expr = self._expression()
        if self._at("OP") and self._peek().text in _ASSIGN_OPS:
            op = self._next().text
            value = self._expression()
            self._expect("OP", ";", "';'")
            return self._node(Assign, expr, op=op, target=expr, value=value)
        if isinstance(expr, Unary) and expr.op in ("++", "--", "post++", "post--"):
            self._expect("OP", ";", "';'")
            return self._node(IncDec, expr, op=expr.op[-2:], target=expr.operand,
                              prefix=not expr.op.startswith("post"))
        self._expect("OP", ";", "';'")
        return self._node(ExprStmt, expr, expr=expr)

    def _if_statement(self) -> If:
        if_tok = self._next()
        self._expect("OP", "(", "'('")
        cond = self._expression()
        self._expect("OP", ")", "')'")
        then = self._statement()
        els: Optional[Stmt] = None
        if self._accept("IDENT", "else"):
            els = self._statement()
        return self._node(If, if_tok, cond=cond, then=then, els=els)

    def _declaration(self) -> VarDecl:
        tok = self._peek()
        is_const = self._accept("IDENT", "const") is not None
        self._skip_precision_qualifiers()
        ty = self._type_at()
        if ty is None:  # pragma: no cover - 调用方已保证
            self._raise("syntax", "声明缺少类型", self._peek())
        self._next()
        name_tok = self._expect("IDENT", what="变量名")
        if self._at("OP", "("):
            self._raise("unsupported_construct:nested_function",
                        f"函数体内不允许再定义函数 `{name_tok.text}`", name_tok)
        self._reject_array_declarator()
        init: Optional[Expr] = None
        if self._accept("OP", "="):
            init = self._expression()
        first = self._node(VarDecl, name_tok, ty=ty, name=name_tok.text, init=init, const=is_const)
        if self._at("OP", ","):
            # 多声明符：折成一个顺序块，语义与逐条声明一致。
            decls: List[Stmt] = [first]
            while self._accept("OP", ","):
                n_tok = self._expect("IDENT", what="变量名")
                self._reject_array_declarator()
                n_init: Optional[Expr] = None
                if self._accept("OP", "="):
                    n_init = self._expression()
                decls.append(self._node(VarDecl, n_tok, ty=ty, name=n_tok.text,
                                        init=n_init, const=is_const))
            self._expect("OP", ";", "';'")
            return self._node(Block, tok, stmts=tuple(decls))
        self._expect("OP", ";", "';'")
        return first

    # ── 表达式 ──
    def _expression(self) -> Expr:
        return self._ternary()

    def _ternary(self) -> Expr:
        cond = self._binary(1)
        if not self._at("OP", "?"):
            return cond
        q = self._next()
        then = self._expression()
        self._expect("OP", ":", "':'")
        els = self._ternary()        # 右结合：a ? b : c ? d : e
        return self._node(Ternary, q, cond=cond, then=then, els=els)

    def _binary(self, min_level: int) -> Expr:
        """优先级爬升：只要下一个运算符结合得不比 ``min_level`` 松就继续。

        右侧递归用 ``level + 1``，因此同级运算符左结合（``a - b - c`` =
        ``(a - b) - c``）。
        """
        left = self._unary()
        while True:
            tok = self._peek()
            level = _BIN_LEVEL.get(tok.text) if tok.kind == "OP" else None
            if level is None or level < min_level:
                return left
            self._next()
            right = self._binary(level + 1)
            left = self._node(Binary, tok, op=tok.text, left=left, right=right)

    def _unary(self) -> Expr:
        tok = self._peek()
        if tok.kind == "OP" and tok.text in ("-", "+", "!", "~", "++", "--"):
            self._next()
            operand = self._unary()
            return self._node(Unary, tok, op=tok.text, operand=operand)
        return self._postfix()

    def _postfix(self) -> Expr:
        expr = self._primary()
        while True:
            tok = self._peek()
            if tok.kind != "OP":
                return expr
            if tok.text == ".":
                self._next()
                member = self._expect("IDENT", what="成员名")
                if not all(c in _SWIZZLE_SETS[0] for c in member.text) and \
                        not all(c in _SWIZZLE_SETS[1] for c in member.text) and \
                        not all(c in _SWIZZLE_SETS[2] for c in member.text):
                    self._raise("unsupported_construct:struct_member",
                                f"成员访问 `.{member.text}` 不是 swizzle（struct 成员不在可译子集内）",
                                member)
                expr = self._node(Swizzle, member, base=expr, components=member.text)
            elif tok.text == "[":
                self._next()
                index = self._expression()
                self._expect("OP", "]", "']'")
                expr = self._node(Index, tok, base=expr, index=index)
            elif tok.text in ("++", "--"):
                self._next()
                expr = self._node(Unary, tok, op="post" + tok.text, operand=expr)
            else:
                return expr

    def _primary(self) -> Expr:
        tok = self._peek()
        if tok.kind in ("FLOAT", "INT", "UINT"):
            self._next()
            ty = {"FLOAT": FLOAT, "INT": INT, "UINT": UINT}[tok.kind]
            return self._node(Literal, tok, text=tok.text, ty=ty)
        if tok.kind == "OP" and tok.text == "(":
            self._next()
            inner = self._expression()
            self._expect("OP", ")", "')'")
            return inner
        if tok.kind == "IDENT":
            if tok.text in ("true", "false"):
                self._next()
                return self._node(Literal, tok, text=tok.text, ty=BOOL)
            ty = self._type_at()
            if ty is not None and self._peek(1).kind == "OP" and self._peek(1).text == "(":
                self._next()
                return self._node(Constructor, tok, ty=ty, args=self._call_args())
            if tok.text in _UNSUPPORTED_KEYWORDS:
                self._reject_unsupported_keyword(tok)
            self._next()
            if self._at("OP", "("):
                return self._node(Call, tok, callee=tok.text, args=self._call_args())
            return self._node(Ident, tok, name=tok.text)
        found = "文件结尾" if tok.kind == "EOF" else repr(tok.text)
        self._raise("syntax", f"表达式出现意外的记号 {found}", tok)

    def _call_args(self) -> Tuple[Expr, ...]:
        self._expect("OP", "(", "'('")
        args: List[Expr] = []
        if not self._at("OP", ")"):
            while True:
                args.append(self._expression())
                if not self._accept("OP", ","):
                    break
        self._expect("OP", ")", "')'")
        return tuple(args)
