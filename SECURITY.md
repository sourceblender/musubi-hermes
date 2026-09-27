# Security Policy

## Supported versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

`musubi-hermes` is pre-1.0. Only the latest `0.1.x` release receives security
fixes. Install from a reviewed commit, and upgrade by installing a newer
reviewed commit.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Report privately, either through GitHub's
[private vulnerability reporting](https://github.com/sourceblender/musubi-hermes/security/advisories/new)
or by email to `ericmey@gmail.com`. Include:

- A clear description of the vulnerability and its impact.
- A reproducer: steps, Hermes version (`hermes --version`), plugin commit, and
  what you observed.
- Whether you intend to disclose, and on what timeline.

You will receive an acknowledgement within 3 business days. We aim to produce a
fix or a mitigation plan within 14 days for high-impact issues.

## What we will not do

- We will not ask for, accept or store bearer tokens, `.env` contents, profile
  paths or other credential material. If your reproducer includes real
  credentials, redact them before sending.
- We will not publish an advisory that includes conversation text or captured
  memory content.

## Scope

In scope:

- Credential handling: a Musubi token read from the wrong profile, written to
  `config.yaml`, logged, or sent anywhere other than the configured Musubi API.
- Profile isolation: one Hermes profile reading or writing another profile's
  outbox, metrics or memory namespace.
- Reporting a memory as stored when it was not verified by readback, or losing
  a queued write without recording it as dead.
- Automatic writes from cron or subagent contexts.

Out of scope:

- Vulnerabilities in Hermes Agent itself. Report those to
  [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent).
- Vulnerabilities in the Musubi server. Report those to the Musubi project.
- Issues that require an attacker who can already read the profile's
  `HERMES_HOME`, or its secrets.
