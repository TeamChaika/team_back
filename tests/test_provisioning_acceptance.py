"""Real PG owner delegation; only external Auth uses the established test adapter."""

import time
from uuid import UUID

import pytest
from psycopg.errors import CheckViolation
from test_platform_sso import pg_repo, sso  # noqa: F401

from app.saas_admin.provisioning_routes import ProvisioningRequests
from app.saas_admin.repository import Problem, digest


@pytest.fixture
def acceptance(sso, tmp_path):  # noqa: F811
    repo, parent = sso[:2]
    with repo.connect(True) as db:
        db.execute(
            "CREATE TABLE runtime_provisioning(company_id uuid PRIMARY KEY, "
            "configuration_version bigint NOT NULL,socket_path text NOT NULL, "
            "state text DEFAULT 'pending',step text,checks jsonb DEFAULT '{}'::jsonb, "
            "error_code text,updated_at timestamptz DEFAULT now())"
        )

    def get(company_id):
        with repo.connect() as db:
            return repo._get(db, company_id)

    repo.get = get
    root = tmp_path / "acceptance"
    service = ProvisioningRequests(repo, str(tmp_path / "r"), acceptance_root=root)
    path = root / ("c_" + UUID(repo.company_id).hex + ".acceptance")
    return repo, parent, service, path


def enqueue(acceptance, **kwargs):
    repo, parent, service, _ = acceptance
    return service.enqueue(repo.company_id, 1, {"id": repo.owner_id}, owner_token=parent, **kwargs)


def test_real_owner_scope_private_file_and_parent_revocation(acceptance):
    repo, parent, _, path = acceptance
    assert enqueue(acceptance)["state"] == "pending"
    token = path.read_text().strip()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert repo.tenant_actor(token, repo.company_id).kind == "platform_owner"
    with repo.connect() as db:
        row = db.execute(
            "SELECT * FROM platform_tenant_sessions WHERE token_hash=%s", (digest(token),)
        ).fetchone()
        assert row["parent_hash"] == digest(parent)
        assert time.time() < row["expires"] <= time.time() + 1800
        assert db.execute("SELECT count(*) n FROM memberships").fetchone()["n"] == 0
    repo.logout(parent)
    with pytest.raises(Problem):
        repo.tenant_actor(token, repo.company_id)


def test_duplicate_keeps_existing_and_retry_replaces_child(acceptance):
    repo, _, _, path = acceptance
    enqueue(acceptance)
    old = path.read_text().strip()
    enqueue(acceptance)
    assert path.read_text().strip() == old
    with repo.connect(True) as db:
        assert db.execute("SELECT count(*) n FROM platform_tenant_sessions").fetchone()["n"] == 1
        db.execute("UPDATE runtime_provisioning SET state='failed'")
    enqueue(acceptance, retry=True)
    assert path.read_text().strip() != old
    with pytest.raises(Problem):
        repo.tenant_actor(old, repo.company_id)


def test_failed_queue_revokes_child_and_restores_file(acceptance):
    repo, _, _, path = acceptance
    enqueue(acceptance)
    old = path.read_text().strip()
    with repo.connect(True) as db:
        db.execute("UPDATE runtime_provisioning SET state='failed'")
        db.execute(
            "ALTER TABLE runtime_provisioning ADD CONSTRAINT reject_pending CHECK(state<>'pending')"
        )
    with pytest.raises(CheckViolation):
        enqueue(acceptance, retry=True)
    assert path.read_text().strip() == old
    assert repo.tenant_actor(old, repo.company_id).kind == "platform_owner"
    with repo.connect() as db:
        assert db.execute("SELECT count(*) n FROM platform_tenant_sessions").fetchone()["n"] == 1


def test_no_parent_and_stale_configuration_cannot_queue(acceptance):
    repo, _, service, path = acceptance
    with pytest.raises(Problem):
        service.enqueue(repo.company_id, 1, {"id": repo.owner_id})
    with pytest.raises(Problem):
        service.enqueue(repo.company_id, 2, {"id": repo.owner_id}, owner_token=acceptance[1])
    assert not path.exists()


def test_acceptance_must_remain_outside_runtime():
    with pytest.raises(ValueError):
        ProvisioningRequests(
            object(), "/private/runtime", acceptance_root="/private/runtime/acceptance"
        )


def test_expired_child_renews_only_at_modules_and_never_extends_parent(acceptance):
    from app.saas_admin.provisioning_acceptance import renew_owner_acceptance

    repo, parent, _, path = acceptance
    enqueue(acceptance)
    token = path.read_text().strip()
    company = repo.get(repo.company_id)
    with repo.connect(True) as db:
        db.execute("UPDATE platform_tenant_sessions SET expires=0")
    with pytest.raises(Problem):
        renew_owner_acceptance(repo, company, token)
    with repo.connect(True) as db:
        db.execute("UPDATE runtime_provisioning SET state='running',step='modules'")
        db.execute(
            "UPDATE sessions SET expires=%s WHERE token_hash=%s",
            (time.time() + 600, digest(parent)),
        )
        parent_expiry = db.execute(
            "SELECT expires FROM sessions WHERE token_hash=%s", (digest(parent),)
        ).fetchone()["expires"]
    assert renew_owner_acceptance(repo, company, token) == parent_expiry
    assert repo.tenant_actor(token, repo.company_id).kind == "platform_owner"
    repo.logout(parent)
    with pytest.raises(Problem):
        renew_owner_acceptance(repo, company, token)


def test_renewal_rejects_disabled_owner_and_changed_version(acceptance):
    from app.saas_admin.provisioning_acceptance import renew_owner_acceptance

    repo, _, _, path = acceptance
    enqueue(acceptance)
    token = path.read_text().strip()
    company = repo.get(repo.company_id)
    with repo.connect(True) as db:
        db.execute("UPDATE runtime_provisioning SET state='running',step='modules'")
    with pytest.raises(Problem):
        renew_owner_acceptance(repo, {**company, "version": 2}, token)
    with repo.connect(True) as db:
        db.execute("UPDATE platform_memberships SET active=false")
    with pytest.raises(Problem):
        renew_owner_acceptance(repo, company, token)
