# Security

## Reporting

If you find a vulnerability (e.g. path that can kill unrelated processes, secret leakage), please open a private security advisory on the GitHub repository or contact the maintainers through the repo’s security contact once published.

## Design notes

- Local AI Monitor runs **locally** on the user’s Mac.
- Audit logs must not contain full command lines or environment secrets.
- Resource actions refuse interactive AI sessions (Grok, Claude CLI, etc.) unless the user explicitly ends them.
