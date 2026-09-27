# Musubi for Hermes Agent

This repository is a Hermes directory plugin for durable Musubi memory. It
registers one memory provider named `musubi`, captures primary-context turns,
offers the `musubi_*` tools, and keeps pending writes in a profile-local SQLite
outbox until the stored object is verified by readback.

## Requirements

- Hermes Agent 0.21.4 or newer, with its Python environment and PyYAML.
- A Musubi API endpoint and a credential scoped to the configured presence.
- One plugin installation per Hermes profile that uses Musubi.

## Install

Use a reviewed, full 40-character commit SHA. Hermes installs the directory at
`$HERMES_HOME/plugins/musubi/` for the selected profile:

```sh
hermes plugins install sourceblender/musubi-hermes --ref <reviewed-40-character-sha> --enable
```

Set `memory.provider: musubi` in that profile's `config.yaml`. Existing profiles
can keep their `musubi` section and credential file:

```yaml
memory:
  provider: musubi
musubi:
  tenant: example
  presence: assistant
  env_file: /path/to/profile-private/musubi.env
  # Optional: seat-specific system-prompt recall instructions.
  recall_guidance: "Recall decisions and people before answering from memory."
```

The credential file is mode 600 and contains `MUSUBI_API_URL` and
`MUSUBI_TOKEN`. Hermes' profile secret scope also supports those names and takes
precedence over file values. Never put the token in `config.yaml`. Each profile
must have its own tenant, presence, credential, and Hermes home.

The provider keeps its existing data paths: `$HERMES_HOME/musubi-outbox.db` and
`$HERMES_HOME/metrics/musubi.prom`. Installing the plugin does not migrate or
delete those files. Back them up before changing a live profile.

## Verify and operate

`hermes musubi status` reports pending, dead, and degraded outbox state without
posting a memory. `hermes memory status` checks that the provider is selected.
After a profile upgrade, run a real primary-context `musubi_remember` and an
exact `musubi_recall` or object readback in that same profile. A queued receipt
means the write is durable locally, not yet stored in Musubi.

The provider skips automatic writes in cron and subagent contexts. It resolves
secrets through Hermes' profile scope and starts its outbox worker with a
context-preserving thread. It does not require `musubi-harness`.

To roll back, install the previous reviewed commit with `--force --ref <sha>`
for each affected profile. Keep the existing outbox and metrics files. Verify
the provider and readback again before declaring the profile healthy.

## Development

The implementation is in `musubi/__init__.py`; the root `__init__.py`,
`plugin.yaml`, `config_schema.py`, and `cli.py` are Hermes discovery surfaces.
Run `pytest -q` for the safe local unit suite and `python3
tests/test_lease_contention.py` for the two-process SQLite lease gate.
`python3 tests/test_upgrade_and_recovery.py` checks migrations and telemetry
using temporary data and a dummy credential. Real-loader contract tests use
Hermes Agent's actual plugin discovery against a temporary `HERMES_HOME`; they
skip outside a Hermes Python environment. Live write/readback is a separate
deploy gate and requires a real profile credential.

See the [Hermes memory-provider plugin guide](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/memory-provider-plugin.md)
for the host interface and installation rules.
