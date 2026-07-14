# Security Policy

## Supported Versions
| Version | Supported |
|---|---|
| latest (`main`) | ✅ |

## Reporting a Vulnerability
Please **do not** open a public issue for security vulnerabilities. Instead,
report them privately:

- Email **edy.cu@live.com**, or
- Use GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability) (Security → Report a vulnerability).

You'll get an acknowledgment within 48 hours and a resolution timeline after
triage. Please give us a reasonable window to patch before public disclosure.

## Notes on This Project's Security Model
Permafrost signs rule bundles with Ed25519 and seals offline event batches with
ECIES (`pynacl`/`SealedBox`). The hash-chained audit log (SHA-256) is designed to
be tamper-evident, not tamper-proof against a fully compromised device — see the
"Residual risk" section of the README for the stated threat-model boundaries.
