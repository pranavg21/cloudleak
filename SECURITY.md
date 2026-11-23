# Security policy

## Reporting a vulnerability

Report privately through [GitHub security advisories](https://github.com/pranavg21/cloudleak/security/advisories/new).
Do not open a public issue, and never attach real billing data; a synthetic reproduction or a description of
the input's shape is enough. Expect an acknowledgement within 5 working days.

Redaction leaks (an identifier surviving `cloudleak redact`), misstated totals, and anything that makes
CloudLeak reach the network or write outside its output paths are treated as security bugs.

## Trust boundary

| Control | How it is enforced |
| --- | --- |
| No network | CloudLeak makes no network calls. DuckDB extension auto-install and auto-load are disabled, so no extension is ever downloaded. No telemetry, no update checks. |
| File access | The DuckDB session may read only the input directories and its own spill directory (`allowed_directories` + `enable_external_access=false`), and its configuration is locked after loading. |
| No credentials | CloudLeak never asks for or uses cloud credentials. Verification commands are printed for a human to run; CloudLeak never runs them. |
| Read-only guidance | Findings carry only read-only commands; a contract check rejects mutating verbs, and every value from an export is shell-quoted. |
| Hostile input | Malformed, ragged, truncated or non-UTF files fail with exit 2 instead of being partially read. `.gz` inputs are stream-checked for truncation and for decompression ratios above 1000:1. Archives are not opened. |
| Hostile content | HTML output is fully escaped, contains no JavaScript and carries a `default-src 'none'` Content Security Policy. CSV output neutralizes formula injection (`=`, `+`, `-`, `@`, tab, CR prefixes on non-numeric cells). |
| Output files | Redaction stages the complete bundle in a private directory. POSIX bundle directories use mode 0700, files and keys use 0600. Publication checks for overlap with the inputs and key. |
| Redaction | An explicit registry limits which structured values can pass through. Other recognized text and tags receive keyed aliases. The tool reopens the final directory or ZIP to check names, headers, CSV values and report metadata before publication. |

## What offline processing does not guarantee

Running locally removes third-party SaaS exposure. It does **not** guarantee that billing data leaves no
trace on the machine:

- DuckDB spills large intermediate results to `--temp-dir` (default: the system temp directory), and the
  operating system may page memory to swap.
- On SSDs, deleting or overwriting a file does not reliably erase it: wear-levelling and over-provisioning
  keep old blocks in cells the OS cannot address (see NIST SP 800-88 on media sanitization).

For regulated data:

1. Use full-disk encryption (FileVault, BitLocker, LUKS).
2. Point `--temp-dir` (or `CLOUDLEAK_TEMP_DIR`) at an encrypted or RAM-backed location such as a tmpfs.
3. Prefer the container with `--network=none --read-only` and a size-limited `--tmpfs /tmp`.
4. Treat HTML/JSON reports like the source export: they contain account IDs and costs unless produced
   from a redacted bundle.
5. Sanitize media at end of life with cryptographic erase or destruction, per your organization's policy.

The bundle preserves numeric costs, credit amounts, dates, recognized currency codes and billing categories.
Repeated aliases, cost patterns, source fingerprints and the key fingerprint can link a bundle to other
data. Opaque service, region, SKU and owner labels can limit analyses that need their original meanings.
Inspect the bundle locally before sharing. Treat diagnostic output from other commands as sensitive.
Only the tool-managed default key directory gets an automatic Git ignore rule. Set appropriate ACLs for
the host platform when POSIX permission bits do not apply.

## Supply chain

- One runtime dependency (`duckdb`).
- Releases are built in GitHub Actions and published to PyPI with trusted publishing (OIDC, no stored
  tokens), with build-provenance attestations and a CycloneDX SBOM attached to each GitHub release.
- The container image is built from the same source and attested.
- CI audits runtime dependencies with `pip-audit` and Dependabot watches dependencies and actions.

Verify a downloaded wheel's provenance with the GitHub CLI:

```bash
gh attestation verify cloudleak_cli-*.whl --repo pranavg21/cloudleak
```
