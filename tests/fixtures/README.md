# Test fixtures

These are synthetic conversations. Names, domains, tokens, hosts, and URLs are fake.
They exist to exercise:

- Slack workspace export layout and threaded replies
- DiscordChatExporter JSON with reply references
- WhatsApp iOS text export with multiline messages
- JSONL exports with explicit thread IDs
- redaction of tokens, emails, and phone numbers
- offline extraction of decisions, steps, pitfalls, and verification

The GitHub token in the Slack fixture is intentionally shaped like a real token
but is not valid. It must never appear in a generated skill.