"""Deterministic dictionary search; every term needs its own matching product token.

No global cache: each search processes only the caller's already authorized catalog.
Numbers, dimensions, percentages and articles never receive fuzzy matching.
"""

import re
import unicodedata

from app.documents.policy import invalid

_WORDS = re.compile(r"[^\W_]+(?:[.,][0-9]+)?%?", re.UNICODE)
_KEYBOARD = str.maketrans("qwertyuiop[]asdfghjkl;'zxcvbnm,.", "йцукенгшщзхъфывапролджэячсмитьбю")
_ALIASES = {"помидор": "томат", "помидоры": "томат", "томаты": "томат", "томат": "томат"}


def normalize(value):
    return unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")


def tokens(value):
    return tuple(t.replace(",", ".") for t in _WORDS.findall(normalize(value)))


def _keyboard_query(value):
    # Decimal punctuation is a numeric qualifier, even in a mistyped keyboard layout.
    return "".join(
        char
        if char in ".,"
        and 0 < index < len(value) - 1
        and value[index - 1].isdigit()
        and value[index + 1].isdigit()
        else char.translate(_KEYBOARD)
        for index, char in enumerate(value)
    )


def _one_edit(left, right):
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        differences = [i for i, (a, b) in enumerate(zip(left, right, strict=False)) if a != b]
        return len(differences) <= 1 or (
            len(differences) == 2
            and differences[1] == differences[0] + 1
            and left[differences[0]] == right[differences[1]]
            and left[differences[1]] == right[differences[0]]
        )
    short, long = sorted((left, right), key=len)
    for index, (a, b) in enumerate(zip(short, long, strict=False)):
        if a != b:
            return short[index:] == long[index + 1 :]
    return True


def _term_score(term, word):
    if term == word or (
        any(c.isdigit() for c in term) and term.removesuffix("%") == word.removesuffix("%")
    ):
        return 0
    if not term.isalpha() or not word.isalpha():
        return None
    if len(term) >= 2 and word.startswith(term):
        return 1
    if _ALIASES.get(term) and _ALIASES[term] == _ALIASES.get(word):
        return 2
    # Retain traditional partial-name search after exact/prefix/alias matches.
    if len(term) >= 3 and term in word:
        return 3
    if min(len(term), len(word)) >= 4 and _one_edit(term, word):
        return 4
    return None


def _score(terms, words):
    options = []
    # Stable term order also stabilizes assignment when aliases overlap.
    for term in sorted(terms):
        matches = [
            (i, score)
            for i, word in enumerate(words)
            if (score := _term_score(term, word)) is not None
        ]
        if not matches:
            return None
        options.append(matches)
    # A bounded augmenting-path assignment avoids factorial work on repeated terms.
    options.sort(key=len)

    def assignment(threshold, forced=None):
        assigned = {}
        scores = {}
        forced_term, forced_position = forced if forced else (-1, -1)
        if forced:
            assigned[forced_position] = forced_term
            scores[forced_term] = 4

        def place(term_index, visited):
            for position, score in sorted(options[term_index], key=lambda pair: (pair[1], pair[0])):
                if score > threshold or position in visited or position == forced_position:
                    continue
                visited.add(position)
                previous = assigned.get(position)
                if previous is None or place(previous, visited):
                    assigned[position] = term_index
                    scores[term_index] = score
                    return True
            return False

        for index in range(len(options)):
            if index != forced_term and not place(index, set()):
                return None
        return (max(scores.values()), sum(scores.values()))

    for threshold in range(4):
        result = assignment(threshold)
        if result is not None:
            return result
    # At most one misspelt term, with all remaining terms exact/prefix/alias/substring.
    results = [
        assignment(3, (index, position))
        for index, matches in enumerate(options)
        for position, score in matches
        if score == 4
    ]
    return min((result for result in results if result is not None), default=None)


def search_products(rows, query, *, limit=50):
    if not isinstance(query, str) or len(query) > 200 or "\x00" in query:
        invalid()
    query = normalize(query.strip())
    terms = tokens(query)
    if len(terms) > 12:
        invalid("Уточните запрос: не больше 12 слов.")
    variants = [terms]
    if query and re.search(r"[a-z]", query) and not re.search(r"[а-я]", query):
        corrected = tokens(_keyboard_query(query))
        if corrected != terms:
            variants.append(corrected)
    ranked = []
    for row in rows:
        name = normalize(row["name"])
        article = normalize(str(row.get("article") or ""))
        words = tokens(row["name"])
        if not terms:
            if query:  # Punctuation-only queries must not expand to the whole catalog.
                continue
            rank = (0, 0, 0)
        elif query == name or (article and query == article):
            rank = (0, 0, 0)
        else:
            scores = [_score(variant, words) for variant in variants]
            scores = [score for score in scores if score is not None]
            if not scores:
                continue
            worst, total = min(scores)
            rank = (1 + worst, total, len(words))
        result = dict(row)
        if row.get("unit") and not row.get("unit_name"):
            result["unit_name"] = row["unit"]
        ranked.append((rank, name, str(row["id"]), result))
    ranked.sort(key=lambda match: match[:3])
    return {"rows": [match[3] for match in ranked[: min(limit, 50)]], "total": len(ranked)}
