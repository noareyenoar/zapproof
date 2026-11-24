# Security

Zapproof is read-only by design. It holds no keys, signs no payments, and opens its
databases in read-only mode. There is no signing code in this repository.

## Reporting

Use GitHub's private vulnerability reporting on this repository
(**Security → Report a vulnerability**). Do not open a public issue for a secret leak
or an exploitable bug.

Please include:

- the affected file or endpoint;
- the smallest request or input that reproduces the issue;
- whether the issue exposes data, alters a score, or causes a denial of service.

## Scope

In scope:

- the hosted API and MCP endpoint;
- identifier parsing and input validation;
- the read-only guarantee (any write path is a bug);
- correction and audit-log integrity;
- prompt-injection paths in the agent skill or tool output.

Out of scope:

- the accuracy of the interpretation (it is rules-based and disclosed as not
  independently validated);
- the truth of on-chain data;
- denial-of-service through ordinary rate limits;
- findings that require a private key the project does not hold.

## Secrets

The repository must never contain `.env`, API tokens, private keys, service-account
files, or databases. `gitleaks` and the staged leak check run before commits.
