# Changelog

All notable changes to this project are documented here. Releases are managed
by [release-please](https://github.com/googleapis/release-please) from
[Conventional Commits](https://www.conventionalcommits.org/).

## 0.1.0

First standalone release of the Musubi memory provider for Hermes Agent, split
out of an in-house tools repository.

### Features

- Hermes directory plugin with `plugin.yaml`, `register(ctx)`, a dashboard config
  schema and a `hermes musubi status` command.
- Credentials resolved through Hermes' profile secret scope, with a legacy
  per-profile `env_file` still supported.
- Outbox worker started with Hermes' context-preserving thread helper. Storage is
  keyed to the profile home it is given.
- Per-seat `recall_guidance` that replaces only the recall paragraph of the
  memory prompt.
- `save_config` through Hermes' own merging config writer.

### Tests

- Real-loader contract suite that installs the plugin into a temporary
  `HERMES_HOME` and loads it through Hermes' memory-provider discovery.
- Outbox fallback, lease reclaim and contention, migration and telemetry
  tests, moved from the original repository.
