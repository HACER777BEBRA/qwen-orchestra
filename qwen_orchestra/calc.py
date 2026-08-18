"""Прямой счёт на ПК: безопасный AST-калькулятор, без eval произвольного кода."""

from __future__ import annotations

import ast
import math
import operator
import re

MAX_EXPR_CHARS = 400
MAX_AST_NODES = 200
MAX_ABS_VALUE = 1e308
MAX_POW_EXP = 64
MAX_FACTORIAL = 170

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_FUNCS = {
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "sinh": math.sinh,
    "cosh": math.cosh,
    "tanh": math.tanh,
    "log": math.log,
    "log10": math.log10,
    "ln": math.log,
    "exp": math.exp,
    "abs": abs,
    "fabs": math.fabs,
    "floor": math.floor,
    "ceil": math.ceil,
    "round": round,
    "pow": pow,
    "min": min,
    "max": max,
    "factorial": math.factorial,
    "degrees": math.degrees,
    "radians": math.radians,
}

_CONSTS = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
}

# Русские глаголы — сами по себе сигнал (можно «посчитай два плюс два» без цифр).
CALC_VERB_RE = re.compile(
    r"(?:"
    r"посчитай|посчитать|вычисли|вычислить|подсчитай|подсчитать|"
    r"сколько\s+будет|чему\s+равно|реши\s+(?:пример|уравнен)|"
    r"найди\s+значен|"
    r"сколько\s+(?:получится|выйдет)|"
    r"процент(?:ов)?\s+от|\bpercent\s+of"
    r")",
    re.IGNORECASE,
)

# EN-глаголы слишком широкие (evaluate this code) — только вместе с числом.
_CALC_VERB_EN_RE = re.compile(
    r"\b(?:calculate|compute|evaluate)\b",
    re.IGNORECASE,
)

_FINANCE_RE = re.compile(
    r"(?:"
    r"сложн\w*\s+процент|"
    r"аннуитет|"
    r"\bpmt\b|\bfv\b|\bpv\b|"
    r"ежемесячн\w*\s+(?:плат[её]ж|взнос|отчислен)|"
    r"отчислен|"
    r"накопить|накоплени|"
    r"compound\s+interest|"
    r"будущ\w+\s+стоимост"
    r")",
    re.IGNORECASE,
)

# Минус только с пробелами: иначе даты/телефоны/версии (10-20, 18-08, 2.5-3).
_NUM = r"\d+(?:[.,]\d+)?"
_BINOP = r"(?:\s*(?:\*\*|[+*/^×])\s*|\s+-\s+|[xх])"
_EXPR_RE = re.compile(_NUM + r"(?:" + _BINOP + _NUM + r")+")

_PCT_RE = re.compile(
    rf"{_NUM}\s*%\s*(?:[*×xх]|от|of)\s*{_NUM}",
    re.IGNORECASE,
)

_PAREN_EXPR_RE = re.compile(
    r"\([^()]*[+*/^×-][^()]*\)(?:\s*[+*/^×-]\s*\(?" + _NUM + r"\)?)*",
)

_FUNC_HINT_RE = re.compile(
    r"\b(?:sqrt|sin|cos|tan|log|ln|exp|abs|factorial)\s*\(",
    re.IGNORECASE,
)

_NUMBER_RE = re.compile(_NUM)

CALC_LINE_RE = re.compile(
    r"(?:CALCULATE|CALC|ВЫЧИСЛИ|ПОСЧИТАЙ)\s*:\s*(.+)",
    re.IGNORECASE,
)

CALC_SYSTEM = """Вычисления делает процессор этого ПК, не ты.
Не считай в уме и не оценивай «примерно».
Составь точное математическое выражение и вызови tool calculate.
В ответе используй только число из результата tool. Можно вызвать calculate несколько раз.
Запись expression: (3+5)*2, sqrt(2), 15%*200, 2**10, sin(pi/2). Не пиши Python-код."""

CALC_SYSTEM_NOTOOLS = """Вычисления делает процессор этого ПК, не ты.
Не считай в уме и не оценивай «примерно».
Если выше уже есть готовые значения с CPU — используй их, не пересчитывай.
Иначе составь формулу и напиши отдельной строкой: CALCULATE: <выражение>
Оркестр подставит точный результат. Не пиши Python-код."""

TOOLS_CALC = [
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": (
                "Точный расчёт выражения на процессоре ПК пользователя. "
                "Вызывай вместо устного счёта: арифметика, проценты, степени, "
                "sqrt/sin/cos/log/factorial, константы pi и e."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": (
                            "Выражение, например (2+3)*4, 15%*200, sqrt(2)+pi. "
                            "Без текста вокруг."
                        ),
                    }
                },
                "required": ["expression"],
            },
        },
    },
]


def format_number(value: float) -> str:
    if not math.isfinite(value):
        return str(value)
    if abs(value - round(value)) < 1e-12:
        as_int = int(round(value))
        if abs(as_int) < 10**15:
            return str(as_int)
    text = f"{value:.12g}"
    if "e" in text.lower():
        return text
    return text.rstrip("0").rstrip(".") if "." in text else text


def _count_nodes(node: ast.AST) -> int:
    return sum(1 for _ in ast.walk(node))


def _check_finite(value: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("not a number")
    num = float(value)
    if not math.isfinite(num) or abs(num) > MAX_ABS_VALUE:
        raise ValueError("overflow")
    return num


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("not a number")
        return _check_finite(float(node.value))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_node(node.operand)
        return value if isinstance(node.op, ast.UAdd) else _check_finite(-value)
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval_node(node.left), _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > MAX_POW_EXP or abs(left) > 1e6):
            raise ValueError("pow too big")
        return _check_finite(_OPS[type(node.op)](left, right))
    if isinstance(node, ast.Name):
        key = node.id.lower()
        if key in _CONSTS:
            return float(_CONSTS[key])
        raise ValueError("unknown name")
    if isinstance(node, ast.Call):
        func = node.func
        if not isinstance(func, ast.Name):
            raise ValueError("bad call")
        name = func.id.lower()
        impl = _FUNCS.get(name)
        if impl is None:
            raise ValueError("unknown func")
        if node.keywords:
            raise ValueError("no kwargs")
        args = [_eval_node(a) for a in node.args]
        if name == "factorial":
            if len(args) != 1 or args[0] < 0 or args[0] != int(args[0]) or args[0] > MAX_FACTORIAL:
                raise ValueError("bad factorial")
            args = [int(args[0])]
        if name == "round" and len(args) == 2:
            args = [args[0], int(args[1])]
        return _check_finite(float(impl(*args)))
    raise ValueError("unsupported expression")


def preprocess(raw: str) -> str:
    expr = (raw or "").strip()
    expr = expr.replace("\u2212", "-").replace("\u00a0", " ").replace("\u00d7", "*")
    expr = expr.replace("^", "**")
    expr = re.sub(r"[×х]", "*", expr)
    expr = re.sub(r"(?<=\d)\s*[xX]\s*(?=\d)", "*", expr)
    expr = re.sub(r"\bплюс\b", "+", expr, flags=re.IGNORECASE)
    expr = re.sub(r"\bминус\b", "-", expr, flags=re.IGNORECASE)
    expr = re.sub(r"\b(?:умножить(?:\s+на)?)\b", "*", expr, flags=re.IGNORECASE)
    expr = re.sub(r"\b(?:разделить(?:\s+на)?|делить(?:\s+на)?)\b", "/", expr, flags=re.IGNORECASE)
    expr = re.sub(r"(\d+(?:\.\d+)?)%\s*(?:от|of)\s*", r"(\1/100)*", expr, flags=re.IGNORECASE)
    expr = re.sub(r"(\d+(?:\.\d+)?)%", r"(\1/100)", expr)
    expr = re.sub(r"(\d+),(\d+)", r"\1.\2", expr)
    expr = re.sub(r"(\d+)!", r"factorial(\1)", expr)
    return expr


def eval_numeric(raw: str) -> float:
    expr = preprocess(raw)
    if not expr or len(expr) > MAX_EXPR_CHARS:
        raise ValueError("bad length")
    tree = ast.parse(expr, mode="eval")
    if _count_nodes(tree) > MAX_AST_NODES:
        raise ValueError("too complex")
    return _eval_node(tree)


def try_eval(raw: str) -> float | None:
    try:
        return eval_numeric(raw)
    except (
        SyntaxError,
        ValueError,
        TypeError,
        ZeroDivisionError,
        OverflowError,
        ArithmeticError,
    ):
        return None


def calculate(args: dict | None = None) -> str:
    """Ollama tool: посчитать expression на CPU."""
    raw = ""
    if isinstance(args, dict):
        raw = str(
            args.get("expression")
            or args.get("expr")
            or args.get("formula")
            or args.get("query")
            or ""
        ).strip()
    elif isinstance(args, str):
        raw = args.strip()
    if not raw:
        return "Ошибка: пустое выражение. Передай expression, например (2+3)*4."
    try:
        value = eval_numeric(raw)
    except ZeroDivisionError:
        return f"Ошибка: деление на ноль в «{raw}»."
    except Exception:  # noqa: BLE001
        return (
            f"Ошибка: не удалось посчитать «{raw}». "
            "Упрости выражение: только числа, + - * / ** % (), sqrt/sin/log, pi, e."
        )
    shown = preprocess(raw)
    return f"{shown} = {format_number(value)}"


TOOL_IMPL_CALC = {
    "calculate": calculate,
}


def need_calc(user_text: str) -> bool:
    """Нужен ли прямой счёт на ПК — детерминированно."""
    t = (user_text or "").strip()
    if len(t) < 2:
        return False
    if _FUNC_HINT_RE.search(t) or _EXPR_RE.search(t) or _PCT_RE.search(t):
        return True
    if CALC_VERB_RE.search(t) or _FINANCE_RE.search(t):
        return True
    if _CALC_VERB_EN_RE.search(t) and _NUMBER_RE.search(t):
        return True
    if re.search(r"сколько|how\s+many|how\s+much", t, re.IGNORECASE):
        if len(_NUMBER_RE.findall(t)) >= 2:
            return True
    return False


def _expr_candidates(user_text: str) -> list[str]:
    t = user_text or ""
    out: list[str] = []
    seen: set[str] = set()
    for raw in (
        *_EXPR_RE.findall(t),
        *_PCT_RE.findall(t),
        *_PAREN_EXPR_RE.findall(t),
    ):
        key = " ".join((raw or "").split())
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    stripped = t.strip()
    # Целиком — только валидная формула, не «10-20» / предложение.
    if stripped and stripped not in seen and len(stripped) <= 80:
        if try_eval(stripped) is not None and (
            _EXPR_RE.fullmatch(stripped)
            or _PCT_RE.fullmatch(stripped)
            or _FUNC_HINT_RE.search(stripped)
            or "(" in stripped
            or re.search(r"[+*/^×%]", stripped)
            or re.search(r"\s-\s", stripped)
        ):
            out.append(stripped)
    return out


def extract_evals(user_text: str) -> list[tuple[str, str]]:
    """Выражения из текста, которые CPU уже посчитал."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in _expr_candidates(user_text):
        key = " ".join(raw.split())
        if key in seen:
            continue
        value = try_eval(raw)
        if value is None:
            continue
        seen.add(key)
        out.append((key, format_number(value)))
        if len(out) >= 8:
            break
    # Вложенные куски («3+5» внутри «(3+5)*2») не требуют отдельной сверки.
    kept: list[tuple[str, str]] = []
    for expr, value in sorted(out, key=lambda p: -len(p[0])):
        if any(expr != other[0] and expr in other[0] for other in kept):
            continue
        kept.append((expr, value))
    kept.reverse()
    return kept


def pick_formula(text: str) -> str | None:
    """Достать из ответа модели одно выражение, которое CPU умеет посчитать."""
    t = text or ""
    hit = CALC_LINE_RE.search(t)
    if hit:
        raw = (hit.group(1) or "").strip().rstrip(".,;")
        if raw and try_eval(raw) is not None:
            return raw
    best: str | None = None
    for line in t.splitlines():
        line = line.strip().strip("`").rstrip(".,;")
        if not line:
            continue
        rhs = line.split("=", 1)[-1].strip().rstrip(".,;") if "=" in line else line
        if try_eval(rhs) is None:
            continue
        if not re.search(r"[+*/^×()%]", rhs) and "**" not in rhs:
            continue
        if best is None or len(rhs) > len(best):
            best = rhs
    if best:
        return best
    pairs = extract_evals(t)
    if not pairs:
        return None
    return max(pairs, key=lambda p: len(p[0]))[0]


def ensure_computed(text: str, expr: str | None = None) -> str:
    """Если в ответе есть формула без числа — дописать результат CPU."""
    body = apply_inline_calculate(text or "")
    formula = (expr or "").strip() or pick_formula(body)
    if not formula:
        return body
    value = try_eval(formula)
    if value is None:
        return body
    shown = format_number(value)
    compact = shown.replace(" ", "")
    if compact and compact in body.replace(" ", "").replace(",", "."):
        return body
    return body.rstrip() + f"\n\n{formula} = {shown}"


def apply_inline_calculate(text: str) -> str:
    """Подставить результаты CPU вместо строк CALCULATE: / ПОСЧИТАЙ:."""

    def _repl(match: re.Match[str]) -> str:
        raw = (match.group(1) or "").strip().rstrip(".,;")
        if not raw:
            return match.group(0)
        return calculate({"expression": raw})

    return CALC_LINE_RE.sub(_repl, text or "")


def calc_context_block(user_text: str) -> str | None:
    """Фрагмент system: готовые результаты с CPU, если выражение удалось вынуть."""
    pairs = extract_evals(user_text)
    if not pairs:
        return None
    lines = ["Прямой счёт на ПК пользователя (не пересчитывай):"]
    for expr, value in pairs:
        lines.append(f"- {expr} = {value}")
    return "\n".join(lines)
