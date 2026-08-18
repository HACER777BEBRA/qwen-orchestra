"""Самопроверка ответа: быстрые детерминированные проверки + LLM-ревью.

Ловит типовые срывы локальных моделей:
  - ответ ушёл на другой язык (китайский/японский/корейский);
  - пустой, обрезанный или зацикленный текст;
  - отказ («не могу», «как языковая модель») вместо ответа —
    по умолчанию включено; выключается/настраивается через settings.selfcheck;
  - явная ошибка или ответ не по вопросу (это уже проверяет LLM-ревьюер).

Результат — `Verdict`: `ok`, список кодов проблем и `hint` — инструкция
для повторной попытки.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .calc import CALC_VERB_RE, extract_evals, format_number, try_eval
from .llm import chat
from .selfcheck_hints import HINTS, SEVERITY

# Скрипты письма: CJK (яп/кит/кор), кириллица, латиница
_CJK_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]"
)
_CYR_RE = re.compile(r"[а-яёА-ЯЁ]")
_LAT_RE = re.compile(r"[a-zA-Z]")

_LOOP_RE = re.compile(r"(.{6,80}?)\1{3,}", re.DOTALL)
_WORD_RE = re.compile(r"\w")
_EMPTY_MARKERS = ("(пустой ответ)", "(empty response)")

# Встроенные маркеры. По умолчанию проверки ВКЛЮЧЕНЫ (бракуют короткий отказ/
# неуверенность). Выключить или заменить списки — через settings.selfcheck.
DEFAULT_REFUSAL_PATTERNS: tuple[str, ...] = (
    "не могу",
    "не в состоянии",
    "не имею возможности",
    "не имею доступа",
    "как языковая модель",
    "как искусственный интеллект",
    "я всего лишь",
    "i cannot",
    "i can't",
    "i am unable",
    "i'm unable",
    "as an ai",
    "as a language model",
)

DEFAULT_UNCERTAIN_PATTERNS: tuple[str, ...] = (
    "не знаю",
    "не уверен",
    "затрудняюсь",
    "уточните",
    "i don't know",
    "i do not know",
)

# Совместимость со старыми импортами
_REFUSAL_PATTERNS = DEFAULT_REFUSAL_PATTERNS
_UNCERTAIN_PATTERNS = DEFAULT_UNCERTAIN_PATTERNS


@dataclass
class SelfcheckRules:
    """Правила быстрых проверок + флаги LLM-вердиктов (из settings.json)."""

    flag_refusal: bool = True
    flag_uncertain: bool = True
    flag_irrelevant: bool = True
    # None → встроенные DEFAULT_*; иначе свой список (пустой = не матчить)
    refusal_patterns: list[str] | None = None
    uncertain_patterns: list[str] | None = None

    def refusal_list(self) -> tuple[str, ...]:
        if self.refusal_patterns is None:
            return DEFAULT_REFUSAL_PATTERNS
        return tuple(p for p in self.refusal_patterns if p)

    def uncertain_list(self) -> tuple[str, ...]:
        if self.uncertain_patterns is None:
            return DEFAULT_UNCERTAIN_PATTERNS
        return tuple(p for p in self.uncertain_patterns if p)

    def to_dict(self) -> dict:
        return {
            "flag_refusal": bool(self.flag_refusal),
            "flag_uncertain": bool(self.flag_uncertain),
            "flag_irrelevant": bool(self.flag_irrelevant),
            "refusal_patterns": (
                None if self.refusal_patterns is None else list(self.refusal_patterns)
            ),
            "uncertain_patterns": (
                None
                if self.uncertain_patterns is None
                else list(self.uncertain_patterns)
            ),
        }


# Активный снимок (проставляет settings.apply_to_runtime)
_RULES = SelfcheckRules()


def get_rules() -> SelfcheckRules:
    return SelfcheckRules(
        flag_refusal=_RULES.flag_refusal,
        flag_uncertain=_RULES.flag_uncertain,
        flag_irrelevant=_RULES.flag_irrelevant,
        refusal_patterns=(
            None if _RULES.refusal_patterns is None else list(_RULES.refusal_patterns)
        ),
        uncertain_patterns=(
            None
            if _RULES.uncertain_patterns is None
            else list(_RULES.uncertain_patterns)
        ),
    )


def set_rules(rules: SelfcheckRules | None) -> None:
    """Применить правила (обычно из settings). None → defaults."""
    global _RULES
    _RULES = rules if rules is not None else SelfcheckRules()

# Арифметику проверяем сами: LLM-ревьюер такие ошибки пропускает
# Глаголы — те же, что у calc (плюс короткие «сколько / how much» без выражения).
_MATH_QUESTION_RE = re.compile(
    r"сколько|посчитай|вычисли|чему равно|реши пример|calculate|how much",
    re.IGNORECASE,
)

REVIEW_MODEL_DEFAULT = "qwen3.5:4b"

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "problem": {
            "type": "string",
            "enum": ["none", "language", "irrelevant", "error", "refusal", "incomplete"],
        },
        "hint": {"type": "string"},
    },
    "required": ["ok", "problem"],
}

REVIEW_SYSTEM = """Ты строгий, но не придирчивый проверяющий ответов ассистента.
Отвечай ТОЛЬКО валидным JSON.

ok=false ставь лишь при ЯВНОЙ проблеме:
- language: ответ не на языке вопроса
- irrelevant: ответ не про то, о чём спросили
- error: явная ошибка в фактах, логике или вычислениях
- refusal: отказ отвечать без причины
- incomplete: ответ обрывается или закрывает лишь часть вопроса

Если ответ по существу и без явных ошибок — ok=true, problem="none".
Стиль, краткость, оформление проблемой НЕ считаются.
hint: одна короткая фраза, что исправить (только при ok=false)."""


@dataclass
class Verdict:
    ok: bool
    problems: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def hint(self) -> str:
        parts = [HINTS[p] for p in self.problems if p in HINTS]
        if self.note and self.note not in {
            "reviewed",
            "rules-only",
            "math-verified",
            "math-patched",
            "review-error",
        }:
            parts.append(self.note)
        return " ".join(parts) or HINTS["error"]

    def summary(self) -> str:
        return ", ".join(self.problems) or ("ok" if self.ok else "unknown")

    def severity(self) -> int:
        return sum(SEVERITY.get(p, 2) for p in self.problems)

    @property
    def checked(self) -> bool:
        """Ответ реально проверен (правила/математика/LLM), а не «ок по умолчанию»."""
        if not self.ok:
            return False
        if self.note == "review-error":
            return False
        return True


def _eval_expr(raw: str) -> float | None:
    return try_eval(raw)


def _fmt_number(value: float) -> str:
    return format_number(value)


def _mentions_number(answer: str, value: float) -> bool:
    """Ищем число как отдельный токен, а не подстроку (`2` ≠ `20` / `12`)."""
    text = (answer or "").replace("\u2212", "-").replace("\u00a0", " ")
    # Разделители тысяч: 1,000 / 1 000 — не трогаем десятичную запятую 2,5
    text = re.sub(r"(?<=\d)[ '’](?=\d{3}(?:\D|$))", "", text)
    text = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", text)
    # Нормализация десятичной запятой для поиска: 2,5 → 2.5
    text_dot = re.sub(r"(\d),(\d)", r"\1.\2", text)
    variants = {_fmt_number(value)}
    if abs(value - round(value)) >= 1e-9:
        variants.add(f"{value:.2f}".rstrip("0").rstrip("."))
        variants.add(f"{value:.4f}".rstrip("0").rstrip("."))
        variants.add(_fmt_number(value).replace(".", ","))
    for v in variants:
        for hay in (text, text_dot):
            if re.search(
                rf"(?<![A-Za-zА-Яа-яЁё0-9.]){re.escape(v)}(?![A-Za-zА-Яа-яЁё0-9.])",
                hay,
            ):
                return True
    return False


def arithmetic_problems(user_text: str, answer: str) -> tuple[list[str], str, bool]:
    """Считаем выражение из вопроса сами и сверяем с ответом.

    Возвращает `(problems, note, verified)`. `verified=True` — ответ содержит
    верное значение; LLM-ревью можно пропустить только для почти чистой арифметики.
    """
    pairs = extract_evals(user_text)
    if not pairs:
        return [], "", False

    verified = False
    for expr, shown in pairs:
        value = _eval_expr(expr)
        if value is None:
            continue
        if not _mentions_number(answer, value):
            return ["error"], f"Правильное значение: {expr} = {shown}.", False
        verified = True
    return [], "", verified


def patch_math_answer(user_text: str, answer: str) -> str | None:
    """Подставить верное число с CPU вместо эскалации. None — патчить нечего."""
    pairs = extract_evals(user_text)
    if not pairs:
        return None
    body = answer or ""
    missing: list[tuple[str, str]] = []
    for expr, shown in pairs:
        value = _eval_expr(expr)
        if value is None:
            continue
        if not _mentions_number(body, value):
            missing.append((expr, shown))
    if not missing:
        return None
    if is_math_only_question(user_text):
        expr, shown = missing[-1]
        return f"{expr} = {shown}"
    extra = "\n".join(f"{expr} = {shown}" for expr, shown in missing)
    return body.rstrip() + "\n\n" + extra


def is_math_only_question(user_text: str) -> bool:
    """Вопрос почти целиком про вычисление — без доп. частей вроде «и объясни»."""
    rest = user_text or ""
    for expr, _ in extract_evals(rest):
        rest = rest.replace(expr, " ")
    rest = CALC_VERB_RE.sub(" ", rest)
    rest = _MATH_QUESTION_RE.sub(" ", rest)
    rest = re.sub(r"[\s.,;:!?()\-+=*/×xх^%]+", " ", rest, flags=re.IGNORECASE).strip()
    return len(rest) < 16


_math_only_question = is_math_only_question


def _has_loop(text: str) -> bool:
    """Зацикливание модели. Разделители (`----`, `|---|`) не считаются."""
    for m in _LOOP_RE.finditer(text):
        unit = m.group(1)
        if not _WORD_RE.search(unit) or len(unit.strip()) < 5:
            continue
        span = m.end() - m.start()
        if span >= 80 or span >= len(text) * 0.25:
            return True
    return False


def _ratio(pattern: re.Pattern[str], text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return len(pattern.findall(text)) / len(letters)


_TRANSLATE_RE = re.compile(
    r"перевед|translate|на\s+английск|in\s+english|на\s+русск|in\s+russian|"
    r"на\s+немецк|на\s+француз|на\s+испан",
    re.IGNORECASE,
)


def language_problems(user_text: str, answer: str) -> list[str]:
    """Ответ должен быть на языке вопроса; иероглифы без запроса — срыв."""
    # Явная просьба сменить язык — не штрафуем за латиницу/другой язык
    if _TRANSLATE_RE.search(user_text or ""):
        user_cjk = bool(_CJK_RE.search(user_text))
        if not user_cjk and _ratio(_CJK_RE, answer) > 0.02:
            return ["cjk"]
        return []

    problems: list[str] = []
    user_cjk = bool(_CJK_RE.search(user_text))
    answer_cjk = _ratio(_CJK_RE, answer)

    if not user_cjk and answer_cjk > 0.02:
        problems.append("cjk")

    user_cyr = _ratio(_CYR_RE, user_text)
    if user_cyr > 0.3 and len(answer) > 20:
        answer_cyr = _ratio(_CYR_RE, answer)
        answer_lat = _ratio(_LAT_RE, answer)
        # латиница может быть кодом/терминами, поэтому порог мягкий
        if answer_cyr < 0.15 and (answer_lat > 0.5 or answer_cjk > 0.02):
            problems.append("language")

    # Симметрично: английский вопрос → ответ почти целиком на русском
    user_lat = _ratio(_LAT_RE, user_text)
    if user_lat > 0.5 and user_cyr < 0.15 and len(answer) > 40:
        answer_cyr = _ratio(_CYR_RE, answer)
        answer_lat = _ratio(_LAT_RE, answer)
        if answer_cyr > 0.5 and answer_lat < 0.2:
            problems.append("language")

    return problems


def content_problems(
    user_text: str,
    answer: str,
    *,
    expect_detail: bool,
    rules: SelfcheckRules | None = None,
) -> list[str]:
    problems: list[str] = []
    text = (answer or "").strip()
    low = text.lower()
    cfg = rules if rules is not None else _RULES

    if not text or low in _EMPTY_MARKERS:
        return ["empty"]

    min_len = 2 if len(user_text.strip()) < 30 or not expect_detail else 40
    if len(text) < min_len:
        problems.append("too_short")

    head = low[:200]
    if cfg.flag_refusal and len(text) < 400:
        refusal = cfg.refusal_list()
        if refusal and any(p in head for p in refusal):
            problems.append("refusal")
    if cfg.flag_uncertain and len(text) < 200:
        uncertain = cfg.uncertain_list()
        if uncertain and any(p in low for p in uncertain):
            problems.append("uncertain")

    if _has_loop(text):
        problems.append("repetition")
    if text.count("```") % 2 == 1:
        problems.append("truncated")

    return problems


def llm_review(
    user_text: str,
    answer: str,
    *,
    model: str = REVIEW_MODEL_DEFAULT,
    timeout: int = 180,
    flag_refusal: bool | None = None,
    flag_irrelevant: bool | None = None,
) -> Verdict:
    """Ревью ответа отдельной моделью: ловит ошибки и уход от вопроса."""
    # Промпт ≤ ~3k токенов оценки — 4096 с запасом, без удержания полного 8k
    review_ctx = 4096
    allow_refusal = _RULES.flag_refusal if flag_refusal is None else bool(flag_refusal)
    allow_irrelevant = (
        _RULES.flag_irrelevant if flag_irrelevant is None else bool(flag_irrelevant)
    )
    messages = [
        {"role": "system", "content": REVIEW_SYSTEM},
        {
            "role": "user",
            "content": f"ВОПРОС:\n{user_text[:2000]}\n\nОТВЕТ:\n{answer[:4000]}",
        },
    ]
    try:
        msg = chat(
            model,
            messages,
            fmt=REVIEW_SCHEMA,
            temperature=0.0,
            num_ctx=review_ctx,
            keep_alive="5m",
            timeout=timeout,
        )
        data = json.loads((msg.get("content") or "").strip())
    except Exception:  # noqa: BLE001 — ревью не должно ломать основной ответ
        # Не помечаем как проверенный: ok=True без reviewed → checked=False снаружи
        return Verdict(True, note="review-error")

    if not isinstance(data, dict) or data.get("ok", True):
        return Verdict(True, note="reviewed")

    problem = str(data.get("problem") or "error").strip()
    if problem in {"none", ""}:
        return Verdict(True, note="reviewed")
    # Если refusal/irrelevant выключены в настройках — LLM-вердикт не бракуем
    if problem == "refusal" and not allow_refusal:
        return Verdict(True, note="reviewed")
    if problem == "irrelevant" and not allow_irrelevant:
        return Verdict(True, note="reviewed")
    if problem not in HINTS:
        problem = "error"
    note = str(data.get("hint") or "").strip()[:200]
    return Verdict(False, [problem], note or "reviewed")


def check(
    user_text: str,
    answer: str,
    *,
    model: str | None = REVIEW_MODEL_DEFAULT,
    expect_detail: bool = True,
    use_llm: bool = True,
    rules: SelfcheckRules | None = None,
) -> Verdict:
    """Полная проверка ответа: сначала дешёвые правила, затем LLM-ревью."""
    cfg = rules if rules is not None else _RULES
    problems = language_problems(user_text, answer)
    problems += content_problems(
        user_text, answer, expect_detail=expect_detail, rules=cfg
    )
    if problems:
        return Verdict(False, problems)

    math_problems, math_note, math_verified = arithmetic_problems(user_text, answer)
    if math_problems:
        return Verdict(False, math_problems, math_note)
    if math_verified and is_math_only_question(user_text):
        return Verdict(True, note="math-verified")

    if use_llm and model and len(answer.strip()) >= 40:
        return llm_review(
            user_text,
            answer,
            model=model,
            flag_refusal=cfg.flag_refusal,
            flag_irrelevant=cfg.flag_irrelevant,
        )

    # Правила прошли, LLM не вызывали — для коротких ответов этого достаточно
    return Verdict(True, note="rules-only")
