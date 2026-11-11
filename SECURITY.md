# Security

## Reporting a vulnerability

Please open a private security advisory on GitHub (Security → Advisories → *Report a vulnerability*) rather than a public issue.

## Audit 2026-11-11 (pre-release, v0.1.0)

**Scope:** all code in `uun/`, `install.sh` and `systemd/`, plus the Python dependency tree (textual 8.2.8, rich 15.0.0, Pygments 2.21.0, markdown-it-py 4.3.0, mdit-py-plugins 0.7.0, linkify-it-py 2.2.0, mdurl 0.1.2, platformdirs 4.14.5, typing_extensions 4.16.0).

**Method:**
- `pip-audit` against the PyPI advisory database, for both the resolved versions and the minimum versions `pyproject.toml` allowed.
- Static analysis with `bandit` and `shellcheck`.
- Manual review of every trust boundary: subprocess execution, `PATH`/working-directory resolution, sudo, config-file trust, network input, terminal/TUI rendering and cache handling.
- A working proof of concept for every code finding, written before patching. Each one is kept as a regression test in `tests/test_security.py`.

**Policy:** fix everything scoring CVSS 3.1 ≥ 4.0. Lower findings were cheap to fix and were fixed too.

### Findings

| ID | Severity | CVSS 3.1 | Finding | Status |
|---|---|---|---|---|
| UUN-01 | **High** | **7.8** `AV:L/AC:L/PR:N/UI:R/S:U/C:H/I:H/A:H` | Code execution from the launch directory (CWE-427) | Fixed |
| UUN-02 | **High** | **7.0** `AV:L/AC:H/PR:N/UI:R/S:U/C:H/I:H/A:H` | `PATH` hijack, including a planted `sudo` that harvests passwords (CWE-426) | Fixed |
| UUN-03 | **Medium** | **6.7** `AV:L/AC:H/PR:L/UI:R/S:U/C:H/I:H/A:H` | Race between checking and reading the sudo-command config; parent directory not checked (CWE-367) | Fixed |
| UUN-04 | **Medium** | **5.3** `AV:N/AC:H/PR:N/UI:R/S:U/C:N/I:H/A:N` | Markup and terminal-escape injection from EoL data (CWE-150 / CWE-74) | Fixed |
| UUN-05 | **Medium** | **5.5** (NVD) | `pyproject.toml` allowed vulnerable dependency versions: CVE-2022-40896, CVE-2023-26302, CVE-2023-26303 | Fixed |
| UUN-06 | Low | 3.7 `AV:N/AC:H/PR:N/UI:N/S:U/C:N/I:L/A:N` | HTTPS → HTTP redirect downgrade in the EoL client (CWE-757) | Fixed |
| UUN-07 | Low | 3.7 `AV:N/AC:H/PR:N/UI:N/S:U/C:N/I:N/A:L` | Slow-drip responses could hang a refresh indefinitely (CWE-400) | Fixed |
| UUN-08 | Low | 3.3 `AV:L/AC:H/PR:L/UI:R/S:U/C:L/I:L/A:N` | pip option injection through crafted distribution names (CWE-88) | Fixed |
| UUN-09 | Low | 3.3 | Pygments CVE-2026-4539 (ReDoS) allowed by the old minimum version | Fixed (minimum raised) |
| UUN-10 | Low | 2.2 `AV:L/AC:H/PR:L/UI:R/S:U/C:N/I:N/A:L` | Corrupt or tampered cache crashed the TUI; themes could read device files (CWE-20) | Fixed |

The resolved dependency tree had **no known vulnerabilities** at audit time. UUN-05 and UUN-09 only affected installs that ended up with older sub-dependencies.

### Details

**UUN-01: code execution from the launch directory.** Probes and checks inherited uun's working directory. Starting `uun` inside an untrusted checkout ran that directory's `django.py` (through the `python3 -c "import django"` probe) and a `./pip/` package (through `python3 -m pip`). The same root cause let `./.npmrc` and `./.cargo/config.toml` steer `npm update -g` and `cargo install-update` (custom registry, `rustc-wrapper`) during updates.
*Fix:* every child process runs with `cwd=/` (`safety.SAFE_CWD`).
*Verified:* the original PoC no longer creates its marker file (`UUN01_*` tests).

**UUN-02: PATH hijack.** Executables were resolved with the user's raw `PATH`. Entries such as `.`, empty or relative paths, and group- or world-writable directories resolve to attacker-controlled files. That included `sudo` itself, so a planted `sudo` could capture the password.
*Fix:*
- `safety.trusted_dirs()` keeps only absolute directories owned by you or root that aren't writable by group or others; when running as root, only root-owned directories.
- `sudo` must be a root-owned setuid binary in a root-owned directory.
- Update commands are resolved to absolute paths and shown verbatim in the confirmation dialog.

**UUN-03: race in the config check.** `assert_safe()` checked the file with `stat()` and then reopened it by path, and it never checked the directory. Anyone who could write to the directory could swap the file between the check and the read and inject sudo commands.
*Fix:* `safety.read_trusted()` resolves the path once, opens it with `O_NOFOLLOW`, checks owner, mode, type and size on the open descriptor, and verifies that every ancestor directory is owned by you or root and isn't writable by others (`/tmp`-style sticky directories are allowed only higher up the chain). This is now used for `config.toml`, `products.toml` and `alternatives.toml`.

**UUN-04: injection.** `DataTable` treats `str` cells as Textual markup, and `notify()` defaults to `markup=True`. EoL data (version strings, labels) could therefore restyle the dashboard or add `[@click=…]` action links. That would require control of endoflife.date, a hostile mirror, or a tampered cache. The same data printed by `--report` could contain raw escape sequences, including an OSC 52 clipboard write.
*Fix:*
- `eol.normalize_product()` is a strict allow-list applied both to network responses and on every cache load: release and version names must match `^[0-9A-Za-z][0-9A-Za-z._+-]{0,31}$`, dates must be ISO dates, and links must be `https://` URLs.
- `safety.clean()` strips C0/C1 control characters, bidi overrides and zero-width characters.
- Every table cell is a `rich.text.Text`, so brackets display literally, and `notify(..., markup=False)` is used throughout.

**UUN-05 / UUN-09: dependency minimum versions.** pip-audit found that the minimum versions previously allowed pulled in Pygments < 2.15.0 (CVE-2022-40896, 5.5) and < 2.20.0 (CVE-2026-4539, 3.3), and markdown-it-py < 2.2.0 (CVE-2023-26302 and CVE-2023-26303, 5.5 each).
*Fix:* `pyproject.toml` now requires `textual>=8.2`, `rich>=14.0`, `pygments>=2.20.0` and `markdown-it-py>=2.2.0`.

**UUN-06 to UUN-08 and UUN-10:**
- Added a redirect handler that refuses anything other than HTTPS.
- Added a 45 s total deadline per request on top of the 2 MB cap.
- pip package names must match a PEP 508-style pattern and are passed after `--`.
- The cache is opened with `O_NOFOLLOW`, ignored if another user owns it or it's writable by others, and re-validated on every load.
- The cache directory is created with mode 0700.
- Theme files must be regular files of at most 256 KiB.

### Out of scope / accepted risk

- **Supply chain of the managed ecosystems.** uun calls apt, dnf, pip, npm, cargo and the rest. Packages they install are trusted according to each tool's own signing and registry model.
- **Theme files aren't permission-checked.** They can't run commands: `bind-key` only maps to a fixed set of UI actions, and updates still need explicit confirmation.
- **Advisory-database coverage.** OSV.dev was unreachable from the audit environment, so dependencies were checked against the PyPI advisory database (PYSEC/GHSA) through pip-audit, cross-checked against NVD. Re-run `pip-audit` in CI before each release.

### Verification

- 28 unit tests pass as root and as an unprivileged user; 16 of them are security regression tests.
- `bandit`: 0 medium or high findings. The remaining low findings are the intentional `subprocess` usage without a shell.
- `shellcheck install.sh`: clean.
- `pip-audit`: no known vulnerabilities in the resolved tree.
- All original proofs of concept re-run against the patched code: blocked.
