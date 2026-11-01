# Contributing

Thanks for considering a contribution. Thread to Skills is a small, local-first tool; the best contributions are focused, tested, and easy to review.

## Ground rules

- **Local-first by default.** Do not add telemetry, analytics, accounts, or a hosted service.
- **Zero runtime dependencies.** The standard library is a feature. If a dependency is unavoidable, discuss it in an issue first.
- **Privacy is part of the product.** New features must not weaken redaction or silently send conversation text to a third party.
- **A skill is a procedure, not a summary.** Keep generated output specific, evidence-backed, and honest about uncertainty.
- **Small PRs win.** One adapter, one hook, or one bug fix per PR is easier to review than a refactor plus a feature.

## Development setup

```bash
git clone https://github.com/fuleinist/thread-to-skill.git
cd thread-to-skill
python -m pip install -e .
python -m unittest discover -s tests -v
```

The package supports Python 3.10+. There is no build step for normal use.

Useful commands:

```bash
t2s --version
t2s doctor
t2s --help
python -m compileall -q src
```

Regenerate screenshots (needs Pillow, development only):

```bash
python -m pip install pillow
python scripts/make_assets.py
```

## Tests

Every behavior change needs a test.

- Adapter changes: add a fixture and a test in `tests/test_adapters.py`.
- Redaction changes: add a positive and a negative case in `tests/test_redact.py`.
- Extraction/rendering changes: add a case in `tests/test_distill.py`.
- CLI changes: add an end-to-end case in `tests/test_cli.py`.
- Validation changes: add a case in `tests/test_validate.py`.

Fixtures must be synthetic. Never commit a real export, a real token, or a real customer domain. The existing fixture token is intentionally invalid.

Run the full suite before opening a PR:

```bash
python -m unittest discover -s tests -v
```

## Adding an adapter

1. Implement `load_<format>(path, gap_minutes, channel, title) -> list[Thread]` in `src/thread_to_skill/adapters/`.
2. Reuse `Message`, `Thread.create`, `group_messages`, and `clean_mentions` from the existing adapters.
3. Add format detection in `adapters/loader.py`.
4. Add a synthetic fixture under `tests/fixtures/`.
5. Add detection and reconstruction tests.
6. Document the format in `docs/how-it-works.md`.

Preserve source timestamps in UTC ISO-8601. If the export has explicit thread IDs, use them; otherwise fall back to quiet-gap windows.

## Adding a provider

1. Subclass `Provider` in `providers.py`.
2. Implement `complete(system, prompt, *, max_tokens, timeout) -> str`.
3. Add it to `get_provider` and `docs/cli.md`.
4. Add a test for request shaping or response parsing. CI must not call the network.

Providers must return one JSON object or raise `ProviderError` with a useful message. Raw failed output should be saved by the caller, not printed with secrets.

## Adding a harness

1. Add the skill root to `harness_skill_dir` in `install.py`.
2. Add the hook writer to `hooks.py`.
3. Document the exact event and output shape in `docs/hooks.md`.
4. Add a test that installs into a temporary project root and asserts the file contents.
5. Keep hook installation idempotent and back up existing files.

If a host cannot inject context, document that limitation honestly instead of pretending the hook does more than it does.

## Code style

- Python 3.10+ syntax; type hints on public functions.
- Standard library only for runtime code.
- Keep functions small and names explicit.
- Prefer deterministic behavior and stable output ordering.
- Do not add a formatter that rewrites unrelated files.

## Commit and PR

- Use a clear imperative subject, for example `fix: preserve Slack thread timestamps`.
- Explain the user-visible behavior and the test that proves it.
- Link the issue when there is one.
- Include screenshots only when the change affects output or docs.
- Do not bump the version in a feature PR; releases are cut separately.

## Release checklist

1. `python -m unittest discover -s tests -v` passes.
2. `python -m compileall -q src` passes.
3. `examples/demo.sh` completes.
4. `t2s validate --all` reports zero errors for the demo.
5. README assets exist and render.
6. `CHANGELOG.md` has an entry.
7. Tag and publish a GitHub release.

## Questions

Open a discussion or an issue. For security reports, follow [SECURITY.md](SECURITY.md).