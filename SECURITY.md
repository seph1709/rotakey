# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| Latest (`main`) | Yes |

## Reporting a vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Open a [GitHub Security Advisory](https://github.com/seph1709/rotakey/security/advisories/new) instead — it is private and only visible to you and the maintainer.

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

You will receive a response within **72 hours**. If the issue is confirmed, a patch will be released as soon as possible.

## Security design notes

- RotaKey binds to `127.0.0.1` (localhost) by default — not reachable from the network.
- API keys are never logged in full — only a masked hint appears in logs.
- Set `ROTAKEY_TOKEN` to require bearer token auth from local clients.
- The Docker image runs as a non-root `rotakey` user.
- `rotakey.yaml` and `.env` are automatically `chmod 600`'d by the installer.
