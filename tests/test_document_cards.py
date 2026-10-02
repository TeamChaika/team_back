"""Document previews preserve quantities and the versioned approval contract."""

import json
from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from app.documents.cards import (
    MAX_HEIGHT,
    STYLES,
    WIDTH,
    document_cards,
    document_rows,
    font,
    paginate,
    quantity,
    wrap,
)
from app.documents.messages import document_messages, html_size
from app.documents.telegram import Telegram, preview


def example(**changes):
    return {
        "id": 76651,
        "number": "DJ076651",
        "version": 2,
        "store": "Мясокомбинат Хангри",
        "counteragent": "Гастро Двор · Кухня",
        "reason": "Проработка",
        "created_by": "Иван Петров",
        "created_at": "2026-10-02T13:40:00+00:00",
        "comment": "На заготовки к выходным",
        "items": [{"name": "Мясо Бедро куриное", "amount": 12}],
        **changes,
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [(12.0, "12"), (1.25, "1,25"), (1e-8, "0,00000001"), (1000000000, "1 000 000 000")],
)
def test_quantities_are_not_rounded_or_given_an_invented_unit(value, expected):
    assert quantity(value) == expected


def test_long_word_wrap_preserves_every_character_and_fits_column():
    value = "ОченьДлинноеНазваниеТовара" * 30
    lines = wrap(value, font(31), 526)
    assert "".join(lines) == value
    assert all(font(31).getlength(line) <= 526 for line in lines)


@pytest.mark.parametrize("kind", ["waybill", "writeoff"])
def test_card_uses_cyrillic_local_date_and_actual_document_fields(kind):
    doc = example()
    rows = document_rows(kind, doc)
    text = "\n".join(row.left for row in rows)
    assert "Мясокомбинат Хангри" in text
    assert "02.10.2026 · 16:40" in text
    assert "Иван Петров" in text
    assert ("КУДА" in text) == (kind == "waybill")
    assert ("ПРИЧИНА СПИСАНИЯ" in text) == (kind == "writeoff")
    assert any(row.left == "Мясо Бедро куриное" and row.right == "12" for row in rows)
    cards = list(document_cards(kind, doc))
    assert len(cards) == 1
    image = Image.open(BytesIO(cards[0][0]))
    assert image.format == "PNG"
    assert image.mode == "RGB"
    assert image.width == WIDTH and 620 <= image.height <= MAX_HEIGHT
    assert len(cards[0][0]) < 10_000_000


def test_pagination_retains_all_items_comments_and_author():
    doc = example(
        items=[{"name": f"Продукт {i}", "amount": i + 0.125} for i in range(85)],
        comment="Длинный комментарий " * 50,
    )
    rows = document_rows("waybill", doc)
    pages = paginate(rows)
    assert len(pages) > 1
    assert [row for page in pages for row in page if row.left or row.right] == [
        row for row in rows if row.left or row.right
    ]
    for page in pages:
        assert 292 + sum(STYLES[row.style][3] for row in page) + 92 <= MAX_HEIGHT
        assert page[-1].style != "label"


def test_units_are_only_shown_when_supplied():
    rows = document_rows("waybill", example(items=[{"name": "Мясо", "amount": 12, "unit": "кг"}]))
    assert any(row.right == "12 кг" for row in rows)


class Bot:
    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at

    def call(self, method, **payload):
        self.calls.append((method, payload))
        if len(self.calls) == self.fail_at:
            raise httpx.ReadTimeout("Uncertain delivery")
        return {"message_id": len(self.calls)}


@pytest.mark.parametrize(
    "kind,prefix,section",
    [("waybill", "Waybill", "transfers"), ("writeoff", "writeoff", "writeoffs")],
)
def test_only_last_page_has_versioned_buttons(kind, prefix, section):
    bot = Bot()
    doc = example(items=[{"name": f"Товар {i}", "amount": i + 1} for i in range(220)])
    settings = SimpleNamespace(dashboard_url="https://dashboard.test")
    result = preview(bot, settings, 101, kind, doc)
    assert result == len(bot.calls) > 1
    assert all(method == "sendMessage" for method, _ in bot.calls)
    assert all("reply_markup" not in payload for _, payload in bot.calls[:-1])
    for _, payload in bot.calls:
        assert "photo" not in payload and "caption" not in payload
        assert payload["parse_mode"] == "HTML"
        assert html_size(payload["text"]) <= 4096
        assert "DJ076651" in payload["text"] and "версия 2" in payload["text"]
    buttons = bot.calls[-1][1]["reply_markup"]["inline_keyboard"]
    assert buttons[0][0]["url"] == f"https://dashboard.test/{section}/documents/76651"
    assert buttons[1][0]["callback_data"] == f"confirm{prefix}:76651:2"
    assert buttons[1][1]["callback_data"] == f"deny{prefix}:76651:2"


def test_partial_send_stops_without_retry_or_sending_approval_buttons():
    bot = Bot(fail_at=2)
    doc = example(items=[{"name": f"Товар {i}", "amount": 1} for i in range(500)])
    with pytest.raises(httpx.ReadTimeout):
        preview(bot, SimpleNamespace(dashboard_url="https://dashboard.test"), 101, "waybill", doc)
    assert len(bot.calls) == 2
    assert all("reply_markup" not in payload for _, payload in bot.calls)


def test_formatted_text_is_sent_as_json_without_an_image():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 44}})

    bot = Telegram("test-token", transport=httpx.MockTransport(respond))
    try:
        assert (
            preview(
                bot,
                SimpleNamespace(dashboard_url="https://dashboard.test"),
                101,
                "waybill",
                example(),
            )
            == 44
        )
        message = requests[-1]
        assert message.url.path.endswith("/sendMessage")
        assert message.headers["Content-Type"] == "application/json"
        payload = json.loads(message.content)
        assert payload["parse_mode"] == "HTML"
        assert payload["link_preview_options"] == {"is_disabled": True}
        assert "<b>CHAIKA" in payload["text"]
        assert "Мясо Бедро куриное — 12" in payload["text"]
        assert "photo" not in payload
        assert payload["reply_markup"]["inline_keyboard"][1][0]["callback_data"] == (
            "confirmWaybill:76651:2"
        )
        bot.call("getUpdates", offset=12, timeout=2)
        assert json.loads(requests[-1].content) == {"offset": 12, "timeout": 2}
    finally:
        bot.close()


@pytest.mark.parametrize("kind", ["waybill", "writeoff"])
def test_text_keeps_actual_fields_and_escapes_untrusted_markup(kind):
    from xml.etree import ElementTree

    doc = example(
        store="Кухня & Бар",
        comment='<a href="https://evil.test">Не ссылка</a>',
        items=[{"name": "Мука <Экстра>", "amount": 0.00001, "unit": "кг"}],
    )
    messages = list(document_messages(kind, doc))
    assert len(messages) == 1
    html = messages[0][0]
    parsed = ElementTree.fromstring("<root>" + html + "</root>")
    assert all(element.tag in {"root", "b"} for element in parsed.iter())
    text = "".join(parsed.itertext())
    assert "Кухня & Бар" in text and doc["comment"] in text
    assert "Мука <Экстра> — 0,00001 кг" in text
    assert "Иван Петров" in text and "02.10.2026 · 16:40" in text
    assert ("Гастро Двор · Кухня" in text) == (kind == "waybill")
    assert ("Проработка" in text) == (kind == "writeoff")


def test_long_escaped_text_and_emoji_are_complete_and_within_message_limits():
    from xml.etree import ElementTree

    name = "&<>😀" * 1300
    comment = "Большой комментарий & < > 😀" * 300
    doc = example(items=[{"name": name, "amount": 1.25}], comment=comment)
    messages = list(document_messages("waybill", doc))
    assert len(messages) > 3
    bodies = []
    for html, _, _ in messages:
        assert html_size(html) <= 4096
        parsed = ElementTree.fromstring("<root>" + html + "</root>")
        # Drop the repeated identity header; remove only added pagination line breaks.
        bodies.append("".join(parsed.itertext()).split("\n\n", 1)[1])
    text = "".join(bodies).replace("\n", "")
    assert name + " — 1,25" in text
    assert comment in text
    assert "Создал: Иван Петров" in text
