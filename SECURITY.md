# Security Policy

## Supported versions

RAYNET Message Logger is currently maintained as version 1.0. Security fixes are applied to the latest code on the `main` branch and included in the version 1.0 download.

| Version | Supported |
| --- | --- |
| 1.0 | Yes |
| Earlier development builds | No |

Users should run the latest version 1.0 code from this repository rather than an older downloaded snapshot.

## Reporting a vulnerability

Please do not disclose suspected vulnerabilities in a public issue, discussion, pull request or social-media post.

Use GitHub's private vulnerability reporting facility on the repository's **Security** tab. If that option is unavailable, contact the maintainer through the [Mathew Levett GitHub profile](https://github.com/mlevett) without including sensitive exploit details in a public message, so a private reporting channel can be arranged.

Please include as much of the following as possible:

- The affected version or commit.
- The deployment environment and relevant configuration.
- A clear description of the vulnerability and its potential impact.
- Reproduction steps or a minimal proof of concept.
- Any suggested remediation.
- Whether the issue has been disclosed to anyone else.

You should receive an acknowledgement within five working days. The report will then be assessed, and you will be told whether it has been accepted, needs more information or is outside the project's scope. Progress updates will normally be provided at least every 14 days while an accepted report remains unresolved.

Please allow time for a fix and coordinated release before publishing details. Good-faith security research that avoids privacy violations, data destruction and disruption of operational use is welcome.

## Security-relevant scope

Reports are particularly useful when they concern:

- Authentication or authorisation bypasses.
- Exposure or alteration of event, message, operator, audit or backup data.
- Injection, cross-site scripting, cross-site request forgery or unsafe file handling.
- WebSocket access-control or session-management weaknesses.
- Export, backup, logo-upload or administrative functions that cross a security boundary.

Operational feature requests, ordinary bugs and deployment-support questions should use the public issue tracker unless they reveal a security weakness.

## Deployment responsibilities

This is a self-hosted application. Deployers are responsible for securing the host and network around it. In particular:

- Do not expose the development server directly to the public internet.
- Use HTTPS through a maintained reverse proxy for any traffic leaving a trusted private network.
- Restrict access with a firewall, trusted LAN or VPN.
- Give every user an individual account and strong, unique password.
- Limit administrator access and promptly disable accounts that are no longer required.
- Protect database files, exported records and backups as operationally sensitive information.
- Keep the operating system, Python runtime, dependencies, reverse proxy and browser clients patched.
- Test restoration from backups and retain them according to the organisation's data-handling policy.

The project cannot guarantee the security of an installation whose host, credentials, network or third-party components have been compromised.
