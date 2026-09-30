import asyncio
from uuid import UUID

import httpx
import pytest
from test_iiko_employees import config

from app.integrations.iiko.employee_write import EmployeeGateway, rejection
from app.integrations.iiko.errors import IikoError


@pytest.mark.parametrize(
    "body,code",
    [
        ("Указанный ПИН-код уже существует.\n", "employee_pin_conflict"),
        ("private PIN=001239", "employee_write_rejected"),
        ("Указанный ПИН-код уже существует. PIN=001239", "employee_write_rejected"),
        ("<html>private PIN=001239</html>", "employee_write_rejected"),
        ("", "employee_write_rejected"),
        ("Указанный ПИН-код уже существует." + " " * 4096, "employee_write_rejected"),
    ],
)
def test_conflict_has_safe_actionable_message_and_preserves_definite_rejection(body, code):
    methods = []

    def upstream(request):
        methods.append(request.method)
        return httpx.Response(409, text=body)

    async def run():
        gateway = EmployeeGateway(config(), transport=httpx.MockTransport(upstream))
        gateway.token = "test-token"
        try:
            with pytest.raises(IikoError) as error:
                await gateway.save(UUID(int=1), {"departmentCodes": ["0001"]})
            assert error.value.code == code
            assert error.value.status_code == error.value.upstream_status_code == 409
            assert not error.value.outcome_unknown
            assert "private" not in str(error.value)
            assert "001239" not in str(error.value)
        finally:
            await gateway.client.aclose()

    asyncio.run(run())
    assert methods == ["POST"]


def test_broken_conflict_body_is_still_definitely_rejected():
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial"
            raise httpx.ReadTimeout("private response")

    error = asyncio.run(rejection(httpx.Response(409, stream=BrokenStream())))
    assert error.status_code == 409
    assert error.code == "employee_write_rejected"
    assert not error.outcome_unknown
    assert "private" not in str(error)


def test_server_failure_remains_ambiguous_even_with_conflict_text():
    error = asyncio.run(rejection(httpx.Response(500, text="Указанный ПИН-код уже существует.")))
    assert error.outcome_unknown
    assert error.upstream_status_code == 500
    assert error.code == "employee_write_rejected"
