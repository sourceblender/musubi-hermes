"""Configuration shown by Hermes' memory provider panel."""

from plugins.memory.config_schema import (
    KIND_TEXT,
    ProviderConfigSchema,
    ProviderField,
)

CONFIG_SCHEMA = ProviderConfigSchema(
    name="musubi",
    label="Musubi",
    fields=(
        ProviderField(key="tenant", label="Tenant", kind=KIND_TEXT, inline=True),
        ProviderField(key="presence", label="Presence", kind=KIND_TEXT, inline=True),
        ProviderField(
            key="env_file",
            label="Profile credential file",
            kind=KIND_TEXT,
            description="Existing mode-600 file containing MUSUBI_API_URL and MUSUBI_TOKEN.",
            inline=True,
        ),
        ProviderField(
            key="recall_guidance",
            label="Recall guidance",
            kind=KIND_TEXT,
            description="Optional seat-specific guidance added to the system prompt.",
        ),
    ),
)
