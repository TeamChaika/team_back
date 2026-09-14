"""Получение и проверка элементов номенклатуры."""

from collections import Counter
from pathlib import Path
from uuid import UUID

import ijson
from pydantic import ValidationError

from app.core.config import BACKEND_DIR, Settings
from app.integrations.iiko.errors import IikoError
from app.schemas.iiko_products import IikoProduct, IikoProductsPage, IikoProductsSnapshot
from app.services.iiko_auth import IikoAuthService
from app.services.local_catalog import LocalCatalog


def read_products(path: Path, max_bytes: int) -> list[IikoProduct]:
    """Читаем по одному объекту, не загружая весь исходный JSON в память."""
    if path.stat().st_size > max_bytes:
        raise IikoError("iiko_products_too_large", "Файл номенклатуры превышает лимит размера.")
    products: list[IikoProduct] = []
    ids: set[UUID] = set()
    try:
        with path.open("rb") as source:
            events = ijson.parse(source)
            if next(events, None) != ("", "start_array", None):
                raise ValueError("Expected a JSON array")
            for item in ijson.items(events, "item"):
                product = IikoProduct.model_validate(item)
                if product.id in ids or product.deleted:
                    raise ValueError("Duplicate ID or unexpected deleted product")
                ids.add(product.id)
                products.append(product)
    except (ijson.JSONError, ValidationError, ValueError, OverflowError):
        # Ошибки парсера могут содержать куски исходного ответа; наружу их не передаём.
        raise IikoError(
            "iiko_products_invalid_response",
            "Ответ номенклатуры повреждён или не соответствует ожидаемой структуре.",
        ) from None
    return products


class IikoProductsService:
    def __init__(
        self, settings: Settings, auth: IikoAuthService, directory: Path | None = None
    ) -> None:
        self._catalog = LocalCatalog(
            settings,
            name="products",
            directory=directory if directory is not None else BACKEND_DIR / ".local/products",
            max_bytes=settings.iiko_products_max_response_bytes,
            download=auth.download_products,
            read=read_products,
            snapshot_model=IikoProductsSnapshot,
            summarize=lambda products: {"type_counts": dict(Counter(p.type for p in products))},
        )

    async def load(self) -> IikoProductsSnapshot:
        return await self._catalog.load()

    async def page(
        self, offset: int, limit: int, snapshot_id: UUID | None = None
    ) -> IikoProductsPage:
        snapshot, items = await self._catalog.page(offset, limit, snapshot_id)
        return IikoProductsPage(snapshot=snapshot, offset=offset, limit=limit, items=items)
