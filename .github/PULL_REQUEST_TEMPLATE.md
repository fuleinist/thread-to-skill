## Summary

What changed and why.

## Test plan

- [ ] `python -m unittest discover -s tests -v`
- [ ] `python -m compileall -q src`
- [ ] `bash examples/demo.sh` (or the PowerShell equivalent)
- [ ] `t2s validate --all` on the demo workspace

## Checklist

- [ ] Runtime code uses only the standard library.
- [ ] I added or updated a test for the behavior change.
- [ ] Fixtures are synthetic and contain no real secrets or customer data.
- [ ] I updated the relevant docs (`README`, `docs/`, `CHANGELOG.md`).
- [ ] Hook changes are idempotent and preserve existing settings.
- [ ] I reviewed generated skills for unredacted sensitive data.

## Screenshots / output

If this changes CLI output or generated skills, include a short before/after.