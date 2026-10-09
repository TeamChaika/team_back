"""Explicit central gateway assembly from the verifier's trusted operator manifest.

Construction performs registry reads and capability validation only. Provisioning
and process startup remain separate operator actions; readiness comes from the
durable runtime registry, never from this configuration.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from starlette.concurrency import run_in_threadpool

from .provisioning_routes import public_targets
from .runtime_operator import RuntimeOperator
from .runtime_process_identity import policy, read_operator_json
from .runtime_registry import RuntimeRegistry
from .server import create_app


def create_deployment_app(
    data_dir, dist_dir=None, origin="http://127.0.0.1:8210", mode="local", *, runtime_config=None
):
    """Keep limited startup unless a private operator file is explicitly supplied."""
    if runtime_config is None:
        return create_app(data_dir, dist_dir, origin, mode)

    configuration = read_operator_json(runtime_config)
    if (
        mode == "production"
        and policy(configuration.get("operator_template", configuration))["mode"] != "linux"
    ):
        raise ValueError("Production requires Linux process isolation")
    if "operator_template" in configuration:
        return create_fleet_deployment_app(configuration, data_dir, dist_dir, origin, mode)
    if Path(configuration["registry_data_directory"]).resolve() != Path(data_dir).resolve():
        raise ValueError("Operator registry data directory must match --data-dir")
    runtime_root = Path(configuration["runtime_root"])
    if not runtime_root.is_absolute() or ".." in runtime_root.parts:
        raise ValueError("Private runtime root must be absolute")
    # Never infer a provider target from the API host, a company domain, or DNS.
    targets = public_targets(configuration.get("public_dns_targets"))
    if set(targets) != {"frontend", "api"}:
        raise ValueError("Explicit frontend and API public DNS targets are required")

    from .edge_peer import EdgePeer

    edge = (
        EdgePeer(configuration["edge_token_file"], configuration.get("edge_trusted_peers", ()))
        if configuration.get("edge_token_file")
        else None
    )
    operator = RuntimeOperator(configuration)
    try:
        operator.repo.validate_ready()
        # Reuse the exact identity-role, database and capability validation used by
        # the separately supervised verifier; this does not start that service.
        operator.verifier_app()
        app = create_app(
            data_dir,
            dist_dir,
            origin,
            mode,
            repository=operator.repo,
            runtime_registry=RuntimeRegistry(operator.repo),
            company_accounts=operator.company_accounts,
            provisioning_root=runtime_root,
            public_dns_targets=targets,
            edge_peer=edge,
            acceptance_root=configuration.get("acceptance_root"),
        )
    except BaseException:
        if operator.repo.auth is not None:
            operator.repo.auth.close()
        raise

    server_lifespan = app.router.lifespan_context
    auth = operator.repo.auth

    @asynccontextmanager
    async def lifespan(app):
        try:
            async with server_lifespan(app):
                yield
        finally:
            await run_in_threadpool(auth.close)

    # server.create_app treats injected repositories as caller-owned.
    app.router.lifespan_context = lifespan
    return app


def create_fleet_deployment_app(configuration, data_dir, dist_dir, origin, mode):
    from .edge_peer import EdgePeer
    from .fleet_discovery import build_fleet_verifier

    template = configuration["operator_template"]
    if Path(template["registry_data_directory"]).resolve() != Path(data_dir).resolve():
        raise ValueError("Fleet registry directory must match --data-dir")
    targets = public_targets(template.get("public_dns_targets"))
    if set(targets) != {"frontend", "api"}:
        raise ValueError("Explicit frontend and API DNS targets required")
    _, repo, accounts = build_fleet_verifier(configuration)
    try:
        repo.validate_ready()
        edge = EdgePeer(template["edge_token_file"], template.get("edge_trusted_peers", ()))
        app = create_app(
            data_dir,
            dist_dir,
            origin,
            mode,
            repository=repo,
            runtime_registry=RuntimeRegistry(repo),
            company_accounts=accounts,
            provisioning_root=template["runtime_root"],
            public_dns_targets=targets,
            edge_peer=edge,
            acceptance_root=template["acceptance_root"],
        )
    except BaseException:
        repo.auth.close()
        raise
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        try:
            async with original(app):
                yield
        finally:
            await run_in_threadpool(repo.auth.close)

    app.router.lifespan_context = lifespan
    return app
