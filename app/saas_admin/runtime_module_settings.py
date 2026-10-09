"""Central encrypted company settings are authoritative for every operator launch."""

DOCUMENT_FIELDS = {
    "commercial_enabled",
    "commercial_submit_enabled",
    "commercial_counterparty_create_enabled",
    "commercial_seller_json",
    "bot_token",
    "bot_username",
    "worker_enabled",
}
ASSISTANT_FIELDS = {
    "provider",
    "api_key",
    "timeweb_agent_id",
    "model",
    "requests_per_hour",
    "max_output_tokens",
}


def apply_company_settings(configuration, company, settings_service):
    """Keep trusted operator controls, replace all customer-owned values from the registry.

    Empty central credentials explicitly clear old operator-file credentials. Never
    use a per-customer JSON value as a fallback when a central group is incomplete.
    """
    for group, fields in (
        ("document_settings", DOCUMENT_FIELDS),
        ("assistant_settings", ASSISTANT_FIELDS),
    ):
        values = configuration.get(group, {})
        if not isinstance(values, dict) or set(values) - fields:
            raise ValueError("Unknown tenant settings field")
    central = settings_service.runtime_settings(company["id"], expected_version=company["version"])
    if set(central) != {"document_settings", "assistant_settings"}:
        raise ValueError("Invalid central company settings")
    document = {
        k: v
        for k, v in configuration.get("document_settings", {}).items()
        if k not in {"commercial_seller_json", "bot_token", "bot_username"}
    }
    assistant = {
        k: v
        for k, v in configuration.get("assistant_settings", {}).items()
        if k in {"requests_per_hour", "max_output_tokens"}
    }
    document.update(central["document_settings"])
    assistant.update(central["assistant_settings"])
    # Optional UUIDs must be absent rather than serialized as empty strings (or
    # "None") in the child environment. The old operator value was discarded
    # above, so omission cannot revive another provider's agent or credentials.
    agent_id = assistant.get("timeweb_agent_id")
    if agent_id is None or (isinstance(agent_id, str) and not agent_id.strip()):
        assistant.pop("timeweb_agent_id", None)
    if set(document) - DOCUMENT_FIELDS or set(assistant) - ASSISTANT_FIELDS:
        raise ValueError("Unknown central company settings field")
    configuration["document_settings"] = document
    configuration["assistant_settings"] = assistant
    return configuration
