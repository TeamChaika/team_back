"""Document store grants never require or imply restaurant analytics access."""

from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.web.administration import EditAccount
from app.web.repository import Repository
from tests.test_portal import USER
from tests.test_restaurant_selection import A, PermissionDatabase, Rows


class DocumentDatabase(PermissionDatabase):
    sections = ["transfers", "writeoffs"]
    active = True
    analytics = False
    all_departments = False
    password_change_required = False

    def execute(self, sql, params=None):
        if "chaika.web_users" in sql:
            return Rows(
                [
                    dict(
                        id=USER,
                        role="manager",
                        display_name="Test",
                        sections=self.sections,
                        all_departments=self.all_departments,
                        password_change_required=self.password_change_required,
                    )
                ]
                if self.active
                else []
            )
        if "chaika.web_department_access" in sql:
            return super().execute(sql, params) if self.analytics else Rows([])
        return super().execute(sql, params)


def repository():
    db = DocumentDatabase()
    repo = object.__new__(Repository)

    @contextmanager
    def connection():
        yield db

    repo.connection = connection
    return repo, db


def test_document_user_enters_shell_without_any_analytics_grant():
    repo, _ = repository()
    scope = repo.portal_scope(USER)
    assert scope.departments == () and scope.store_ids == () and not scope.unrestricted
    assert repo.metadata(scope) == {
        "user": {**scope.user, "id": str(USER)},
        "departments": [],
        "sales_dates": [],
        "balance_dates": [],
    }
    with pytest.raises(HTTPException) as failure:
        repo.scope(USER)
    assert failure.value.status_code == 403


@pytest.mark.parametrize("sections", [[], ["deposits"], ["transfers"], ["writeoffs"]])
def test_shell_does_not_grant_missing_restaurant_access(sections):
    repo, db = repository()
    db.sections = sections
    assert repo.portal_scope(USER).departments == ()


def test_existing_document_analytics_grants_are_preserved():
    repo, db = repository()
    db.analytics = True
    assert A in repo.portal_scope(USER).ids
    db.analytics = False
    db.all_departments = True
    assert repo.portal_scope(USER).unrestricted


def test_other_analytics_sections_still_require_restaurants():
    repo, db = repository()
    db.sections = ["transfers", "sales"]
    with pytest.raises(HTTPException) as failure:
        repo.portal_scope(USER)
    assert failure.value.status_code == 403
    with pytest.raises(ValidationError):
        EditAccount(display_name="Test", sections=db.sections, revision=1)


def test_document_only_account_can_be_saved_without_analytics_grants():
    account = EditAccount(display_name="Test", sections=["transfers", "writeoffs"], revision=1)
    assert not account.all_departments and account.department_ids == []


def test_disabled_document_user_is_rejected():
    repo, db = repository()
    db.active = False
    with pytest.raises(HTTPException) as failure:
        repo.portal_scope(USER)
    assert failure.value.status_code == 403
