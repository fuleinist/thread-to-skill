# Captured demo output

These files are the real output of the commands in [`demo.sh`](../demo.sh) and
[`demo.ps1`](../demo.ps1), generated from the synthetic fixtures in
`tests/fixtures/`.

- `01-init.txt` — workspace creation
- `02-ingest.txt` — Slack export ingestion with redaction counts
- `03-list.txt` — five distilled skills
- `04-suggest.txt` — a prompt matching the testing skill
- `05-validate.txt` — Agent Skills validation
- `06-install.txt` — install into Claude Code and cross-agent roots
- `07-hooks.txt` — Claude Code, Cursor, and DeepSeek bridge hook files
- `skill-example.md` — the full generated `SKILL.md` for the testing skill

The fixtures are deliberately small and synthetic. Names, domains, tokens, and
URLs are fake. Regenerate the screenshots with:

```bash
python -m pip install pillow
python scripts/make_assets.py
```