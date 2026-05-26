# Security Policy

Mnemo is a local stdio MCP server. It stores local project memory, can inspect source files under the configured workspace root for symbol lookup, and now supports portable Memory Packs for local-first memory export/import workflows.

## Supported versions

Mnemo is still pre-1.0. Security fixes are provided for the current public line only.

| Version | Supported |
| --- | --- |
| 0.21.x | Yes |
| < 0.21 | No |

## Reporting issues

Please open a private security advisory or contact the repository owner if you find a vulnerability that could:

- expose local files outside the configured workspace root
- bypass workspace-root restrictions
- leak stored memory unexpectedly
- leak signing, verification, or local HMAC secrets
- leak source database memory IDs through Memory Pack artifacts or action outputs
- import untrusted Memory Pack content into the default local namespace without explicit promotion
- bypass quarantine/trusted namespace retrieval boundaries
- bypass Memory Pack content-hash or signature validation
- corrupt or delete memory outside documented soft-delete/maintenance behavior

## Local data and repository hygiene

Mnemo memory files, SQLite databases, lock files, synthetic test outputs, and Memory Pack exports may contain project-sensitive information.

Do not commit or publish local runtime state unless it is intentionally curated example data. In normal use, keep these out of source control:

- `state/`
- `*.sqlite`
- `*.sqlite.lock`
- `memory.json`
- `memory.json.lock`
- `_test_results/`
- generated Memory Pack ZIPs
- local signing or verification secrets

## Memory Packs security model

Memory Packs are portable artifacts. Treat every pack as potentially sensitive and untrusted until inspected.

Current protections include:

- ZIP structure and path-safety validation
- required-member validation
- manifest content-hash verification
- source memory ID leak checks
- baseline redaction before export
- quarantine import for unsigned, unverified, unknown, or cautious imports
- trusted import only for verified trusted signers
- explicit manual promotion before imported content enters local memory

Important boundaries:

- Quarantine imports are stored under `pack:quarantine:<pack_id>`.
- Trusted imports are stored under `pack:trusted:<pack_id>`.
- Imported rows keep `origin=imported`.
- Default retrieval excludes both quarantine and trusted imported rows.
- `include_quarantine=true` is required for quarantine rows.
- `include_imported=true` is required for trusted imported rows.
- `pack_promote` is the explicit action that creates local promoted memory.

## Redaction limitations

Memory Packs use a baseline-v1 redaction ruleset. It is a safety layer, not comprehensive DLP.

Before sharing a pack externally, inspect it and review whether the redaction output is sufficient for the project. Do not assume all secrets, personal data, proprietary code, or sensitive business context are removed automatically.

## Signing and trust limitations

Mnemo currently implements local HMAC signing with `hmac-sha256-local-v1`.

This is not public-key signing and does not provide non-repudiation. The same shared secret is used to sign and verify. Anyone with the secret can produce signatures that verify under that secret.

Current limitations:

- no public-key identity
- no persistent secret store
- no key revocation
- no remote key discovery
- secret distribution is out-of-band

Secret handling expectations:

- Do not commit signing or verification secrets.
- Do not place secrets in Memory Pack contents.
- Do not share secrets through issue reports or public logs.
- Rotate out-of-band shared secrets if they may have been exposed.

## Expected boundaries

- `lookup_symbol` and related file inspection should stay under `MNEMO_WORKSPACE_ROOT`.
- Mnemo should not make network calls.
- Memory Pack inspection should not extract ZIP contents to arbitrary filesystem paths.
- Memory Pack import should be atomic and should not partially import rows on failure.
- Trusted import should not bypass review/promotion or insert rows directly into the local namespace.
- Optional Agent Salience diagnostics should not require network access.
