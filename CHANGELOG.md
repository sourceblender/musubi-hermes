# Changelog

All notable changes to this project are documented here. Releases are managed
by [release-please](https://github.com/googleapis/release-please) from
[Conventional Commits](https://www.conventionalcommits.org/).

## [0.2.0](https://github.com/sourceblender/musubi-hermes/compare/v0.1.0...v0.2.0) (2026-09-27)


### Features

* **hermes-musubi:** RET-003 pass-through conformance (Yua 12:45:46 [#5](https://github.com/sourceblender/musubi-hermes/issues/5)) ([7f49bc2](https://github.com/sourceblender/musubi-hermes/commit/7f49bc2655fecd38d35a3addcbc0a39348ef9013))
* **musubi:** add durable Hermes memory CLI ([4a3a8e3](https://github.com/sourceblender/musubi-hermes/commit/4a3a8e30a6f5f5056272980c6c9e74ecd8b28fe5))


### Bug Fixes

* **hermes-musubi:** preserve RET-003 retrieval evidence ([#1](https://github.com/sourceblender/musubi-hermes/issues/1)) ([d9f5fdb](https://github.com/sourceblender/musubi-hermes/commit/d9f5fdb5e7235e612605d2f1231e8197b81266bb))
* **hermes-musubi:** Yua 2026-07-13 12:59:30 BLOCKER B (DQ-001 strict xfail) ([3aaacf6](https://github.com/sourceblender/musubi-hermes/commit/3aaacf6255ba4cc7ae0b732b2571282d5fbee25e))
* **musubi:** bind CLI retries to operation identity ([69eaec5](https://github.com/sourceblender/musubi-hermes/commit/69eaec5ceb7ad5bff9fd778f879fb34b1bdde9a5))
* **musubi:** preserve RET-007 degradation warnings in musubi_recall ([#35](https://github.com/sourceblender/musubi-hermes/issues/35)) ([a644e68](https://github.com/sourceblender/musubi-hermes/commit/a644e681aeccd10053b5c65f81b850dfc9a8b5f8))
* **musubi:** scope CLI idempotency to unresolved writes ([875a980](https://github.com/sourceblender/musubi-hermes/commit/875a980fd7ea4894555c7603a1ff591b369b8113))
* **retrieve:** DQ-001 Hermes adapter truncation parity ([#6](https://github.com/sourceblender/musubi-hermes/issues/6)) ([7b81be8](https://github.com/sourceblender/musubi-hermes/commit/7b81be809f16030ffe0fac436bcc413ab9cea702))


### Documentation

* **adapt001:** establish exact tests-first red contract and slice constraints for CLI adapter (Issue [#3](https://github.com/sourceblender/musubi-hermes/issues/3)) ([a667783](https://github.com/sourceblender/musubi-hermes/commit/a667783428564961ae4afc3846daf73f2f55e32d))
* set up musubi-hermes as a public open-source project ([#4](https://github.com/sourceblender/musubi-hermes/issues/4)) ([64b9201](https://github.com/sourceblender/musubi-hermes/commit/64b92013e6b55c4123372536b808293bb51bf4f7))

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
