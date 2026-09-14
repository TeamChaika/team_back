"""Keep every source line even when historical iiko invoices repeat num."""

from collections import Counter
from collections.abc import Iterator


def numbered_invoice_items(items: list[dict]) -> Iterator[tuple[dict, int]]:
    occurrences: Counter[int] = Counter()
    for item in items:
        occurrences[item["num"]] += 1
        yield item, occurrences[item["num"]]
