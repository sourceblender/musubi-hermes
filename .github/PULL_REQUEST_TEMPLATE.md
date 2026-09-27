## What this PR does

<!-- Describe the change. Link an issue if one exists. -->

## Why

<!-- The problem, and why this is the right fix. -->

## Contract impact

- [ ] No change to the provider name `musubi`
- [ ] No change to the `musubi:` config keys
- [ ] No change to the data paths (`musubi-outbox.db`, `metrics/musubi.prom`)
- [ ] No change to the tool names (`musubi_remember`, `musubi_recall`)
- [ ] `queued` is still never reported as stored before readback by id

If anything above changed, describe the migration for existing profiles:

## Checklist

- [ ] `python -m pytest -q` is green
- [ ] `python tests/test_lease_contention.py` and `python tests/test_upgrade_and_recovery.py` pass
- [ ] If the provider, registration, config, secrets, threads or storage changed:
      `tests/contract` run with Hermes' own Python, and the Hermes version is noted here
- [ ] No credentials, real hostnames, profile paths or memory text in the diff
