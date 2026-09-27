# Contributing

Thanks for your interest in `musubi-hermes`. It is the Hermes Agent host
plugin in the `musubi-*` family: a directory plugin that registers one memory
provider, `musubi`, through Hermes' own memory-provider loader. It talks to the
Musubi API directly over HTTP. It has no Python dependencies and does not use
`musubi-harness`.

## Ground rules

1. **Keep the contract existing profiles depend on.** These are stable, and a
   change to any of them is a breaking change: the provider name `musubi`; the
   `musubi:` config section (`tenant`, `presence`, `env_file`, `api_url`,
   `recall_guidance`); the data paths `$HERMES_HOME/musubi-outbox.db` and
   `$HERMES_HOME/metrics/musubi.prom`; and the tool names `musubi_remember` and
   `musubi_recall`.
2. **Follow Hermes' plugin rules.** Read credentials through
   `agent.secret_scope.get_secret`, never `os.environ`. Start background work
   through `agent.memory_provider.spawn_context_thread`, never a bare thread.
   Key storage to the `hermes_home` passed to each call. Make no network calls
   in `is_available()`. See the
   [Hermes memory-provider plugin guide](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/memory-provider-plugin.md).
3. **`queued` and `stored` stay distinct.** A local outbox row is a durable
   promise. A write counts as stored only after the object has been read back
   by id with a matching namespace and content hash. Do not report success
   before readback.
4. **Only primary contexts write.** Cron and subagent contexts must never
   start the outbox worker or capture a turn automatically.
5. **No new runtime dependencies.** The plugin must keep loading from a plain
   directory in any Hermes environment.

## Development setup

```bash
git clone https://github.com/sourceblender/musubi-hermes
cd musubi-hermes
python -m venv .venv
source .venv/bin/activate
pip install pytest pyyaml
```

## Before opening a PR

Run the same checks CI runs:

```bash
python -m pytest -q
python tests/test_lease_contention.py
python tests/test_upgrade_and_recovery.py
```

The contract suite in `tests/contract/` loads the plugin through Hermes' real
memory-provider discovery against a temporary `HERMES_HOME`. Outside a Hermes
Python environment it skips. **If your change touches the provider,
registration, config, secrets, threads or storage, run it with Hermes' own
Python and say so in the PR:**

```bash
MUSUBI_HERMES_PLUGIN_DIR="$PWD" \
  /path/to/hermes-agent/venv/bin/python -m pytest tests/contract -q
```

State which Hermes version you ran it against (`hermes --version`). A skipped
contract suite is not a pass.

Never commit real credentials, hostnames, profile paths or captured memory
text. Tests use dummy tokens and `example.test` hosts.

## Pull request flow

1. Open a PR from a topic branch against `main`.
2. Use a [Conventional Commit](https://www.conventionalcommits.org/) title
   (`fix:`, `feat:`, `docs:`, `test:`, `chore:`). It drives the release notes.
3. CI must be green, and a maintainer reviews it before merge.
4. PRs are squash-merged.

## Releases

Releases are managed by release-please. Conventional Commits on `main` drive a
release PR that bumps `pyproject.toml`, `plugin.yaml` and `CHANGELOG.md`.
Merging it tags `v<X.Y.Z>` and publishes a GitHub Release of the plugin source.
There is no PyPI package. Hermes installs this repository as a directory at a
reviewed commit.

## Security issues

Please do not open a public issue. See [SECURITY.md](SECURITY.md).
