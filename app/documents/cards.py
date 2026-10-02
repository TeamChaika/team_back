"""Private, deterministic Telegram document images; no external image service."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

WIDTH, MAX_HEIGHT = 960, 1440
LEFT, RIGHT = 64, 896
BODY_TOP, FOOTER = 292, 92
BACKGROUND, TEXT, MUTED = "#101e2d", "#eef4fb", "#91a7bb"
TEAL, BORDER = "#48c8c5", "#2b475b"
STYLES = {
    "label": (24, 600, MUTED, 42),
    "body": (34, 600, TEXT, 47),
    "item": (31, 500, TEXT, 44),
    "comment": (30, 500, "#a4e5e4", 45),
    "meta": (25, 400, MUTED, 38),
    "space": (24, 400, MUTED, 22),
    "arrow": (28, 600, TEAL, 42),
    "rule": (24, 400, MUTED, 24),
}


@lru_cache(maxsize=24)
def font(size: int, weight: int = 500) -> ImageFont.FreeTypeFont:
    face = ImageFont.truetype(str(Path(__file__).parent / "assets" / "Manrope.ttf"), size)
    face.set_variation_by_axes([weight])
    return face


def wrap(text: str, face: ImageFont.FreeTypeFont, width: int) -> list[str]:
    """Wrap even an unbroken product name without clipping or dropping characters."""
    lines = []
    for paragraph in str(text).splitlines() or [""]:
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}" if current else word
            if face.getlength(candidate) <= width:
                current = candidate
                continue
            if current:
                lines.append(current)
                current = ""
            for char in word:
                if current and face.getlength(current + char) > width:
                    lines.append(current)
                    current = ""
                current += char
        if current:
            lines.append(current)
    return lines or [""]


def quantity(value) -> str:
    # Keep the document quantity, including tiny fractions; never invent a unit.
    number = format(Decimal(str(value)), "f")
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    whole, dot, fraction = number.partition(".")
    whole = f"{int(whole):,}".replace(",", " ")
    return whole + ("," + fraction if dot else "")


@dataclass(frozen=True)
class Row:
    left: str = ""
    right: str = ""
    style: str = "body"

    @property
    def height(self) -> int:
        return STYLES[self.style][3]


def document_rows(kind: str, doc: dict) -> list[Row]:
    rows: list[Row] = []

    def add(text, style="body", width=RIGHT - LEFT):
        size, weight, _, _ = STYLES[style]
        rows.extend(Row(line, style=style) for line in wrap(str(text), font(size, weight), width))

    add("ОТКУДА" if kind == "waybill" else "СКЛАД", "label")
    add(doc["store"])
    rows.append(Row("↓", style="arrow") if kind == "waybill" else Row(style="space"))
    add("КУДА" if kind == "waybill" else "ПРИЧИНА СПИСАНИЯ", "label")
    add(doc["counteragent"] if kind == "waybill" else doc["reason"])
    rows.append(Row(style="rule"))
    rows.append(Row(f"Состав · {len(doc['items'])}", "Количество", "label"))
    for item in doc["items"]:
        amount = quantity(item["amount"])
        # The current catalog contains names only, so real rows have no unit yet.
        if item.get("unit"):
            amount += " " + str(item["unit"])
        names = wrap(item["name"], font(31), 526)
        amounts = wrap(amount, font(31), 266)
        for i in range(max(len(names), len(amounts))):
            rows.append(
                Row(
                    names[i] if i < len(names) else "",
                    amounts[i] if i < len(amounts) else "",
                    "item",
                )
            )
        rows.append(Row(style="rule"))
    if doc.get("comment", "").strip():
        add("КОММЕНТАРИЙ", "label")
        add(doc["comment"], "comment", RIGHT - LEFT - 36)
        rows.append(Row(style="space"))
    add("Создал: " + doc["created_by"], "meta")
    if doc.get("created_at"):
        created = datetime.fromisoformat(doc["created_at"])
        if created.tzinfo is not None:
            created = created.astimezone(ZoneInfo("Europe/Simferopol"))
        add(created.strftime("%d.%m.%Y · %H:%M"), "meta")
    return rows


def paginate(rows: list[Row]) -> list[list[Row]]:
    pages: list[list[Row]] = [[]]
    used = 0
    available = MAX_HEIGHT - BODY_TOP - FOOTER
    for i, row in enumerate(rows):
        # Keep section headings and the first content line together.
        needed = row.height
        if row.style == "label" and i + 1 < len(rows):
            needed += rows[i + 1].height
        if pages[-1] and used + needed > available:
            pages.append([])
            used = 0
        if not pages[-1] and row.style in {"space", "rule"}:
            continue
        pages[-1].append(row)
        used += row.height
    return pages


def render_page(kind: str, doc: dict, rows: list[Row], page: int, total: int) -> bytes:
    height = max(620, BODY_TOP + sum(row.height for row in rows) + FOOTER)
    image = Image.new("RGB", (WIDTH, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((16, 16, WIDTH - 17, height - 17), radius=30, outline=BORDER, width=2)
    draw.text((LEFT, 56), "C H A I K A", font=font(33, 800), fill=TEAL)
    draw.text((RIGHT, 65), f"Версия {doc['version']}", font=font(23), fill=MUTED, anchor="ra")
    draw.text(
        (LEFT, 116),
        "ПЕРЕМЕЩЕНИЕ" if kind == "waybill" else "СПИСАНИЕ",
        font=font(23, 600),
        fill=MUTED,
    )
    title = ("Накладная" if kind == "waybill" else "Списание") + " №" + doc["number"]
    size = 46
    while font(size, 800).getlength(title) > RIGHT - LEFT and size > 28:
        size -= 1
    draw.text((LEFT, 155), title, font=font(size, 800), fill=TEXT)
    draw.ellipse((LEFT, 232, LEFT + 16, 248), fill="#ffbd53")
    draw.text((LEFT + 30, 220), "На согласовании", font=font(29, 600), fill="#ffbd53")
    y = BODY_TOP
    for row in rows:
        size, weight, color, line_height = STYLES[row.style]
        if row.style == "rule":
            draw.line((LEFT, y + 12, RIGHT, y + 12), fill=BORDER, width=1)
        elif row.style != "space":
            x = LEFT
            if row.style == "comment":
                draw.rectangle((LEFT, y, RIGHT, y + line_height), fill="#143239")
                draw.rectangle((LEFT, y, LEFT + 3, y + line_height), fill=TEAL)
                x += 18
            draw.text((x, y + 2), row.left, font=font(size, weight), fill=color)
            if row.right:
                draw.text(
                    (RIGHT, y + 2), row.right, font=font(size, weight), fill=color, anchor="ra"
                )
        y += line_height
    draw.text((LEFT, height - 63), "Документы сети", font=font(22), fill=MUTED)
    draw.text((RIGHT, height - 63), f"{page} / {total}", font=font(22), fill=MUTED, anchor="ra")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def document_cards(kind: str, doc: dict):
    """Yield one in-memory image at a time; never persist private documents on disk."""
    pages = paginate(document_rows(kind, doc))
    for index, rows in enumerate(pages, 1):
        yield render_page(kind, doc, rows, index, len(pages)), index, len(pages)
