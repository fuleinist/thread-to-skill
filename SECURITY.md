# Security

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | Yes |
| < 0.1 | No |

## Reporting a vulnerability

Please use GitHub’s private vulnerability reporting on this repository:

**Security → Report a vulnerability**

Do not open a public issue for a suspected vulnerability. Include:

- affected version and operating system
- a minimal reproduction, with secrets removed
- the expected and actual behavior
- whether the issue requires a malicious export, a malicious hook file, or a local attacker

You will get an acknowledgement as soon as possible. Please allow time for a fix before public disclosure.

## Scope

In scope:

- redaction bypasses that leave common secrets in `threads/`, `SKILL.md`, or `references/thread.md`
- path traversal or unintended file writes from a malicious export path, slug, or filename
- command injection through the `command` provider or hook installation
- hook files that overwrite unrelated settings or run untrusted content
- generated skills that contain unredacted secrets the validator should have caught
- denial of service from a malformed export that is cheap to trigger

Out of scope:

- vulnerabilities in Claude Code, Cursor, OpenClaw, Hermes Agent, DeepSeek Harness, or an LLM provider
- the content of a conversation the operator chose to ingest
- a user running `t2s init --no-redact` on sensitive data
- an operator enabling a generated hook without reviewing it

## Security model

Thread to Skills is a local CLI, not a service.

- It reads files the user points it at and writes inside the chosen workspace.
- Offline mode makes no network calls.
- LLM mode sends the **redacted** thread to the configured provider. That provider’s terms apply.
- Hooks execute `t2s` as the current user. Hook files are local code; review them before enabling them in a shared host.
- Redaction is best-effort. It is not a guarantee that a document is safe to publish.

## Hardening recommendations

- Keep the workspace private (`chmod 700` on Unix where appropriate).
- Review `SKILL.md` and `references/thread.md` before sharing.
- Run `t2s validate --all` before distributing a skill.
- Use a dedicated API key with minimal scope for LLM providers.
- Do not commit `.thread2skill/` to a public repository unless you have reviewed every generated skill.
- Treat a generated skill like a pull request from an untrusted author: it can contain instructions, commands, and claims that need review.