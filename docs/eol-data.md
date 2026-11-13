# Monthly EoL data bundles

uun can run on machines with no internet access by importing a **signed monthly snapshot** of [endoflife.date](https://endoflife.date) data. The `eol-data` workflow builds it on the 3rd of every month at 04:23 UTC and publishes it as release `eol-YYYY-MM`. That release is never marked Latest, so app releases stay on top.

| Asset | What it is |
| --- | --- |
| `uun-eol-YYYY-MM.json` | Every endoflife.date product, normalized with uun's own allow-list, as canonical JSON (sorted keys, ASCII only) |
| `uun-eol-YYYY-MM.json.sig` | OpenSSH signature: namespace `uun-eol`, principal `eol@uun` |
| `SHA256SUMS` | Checksums of both |

## Verifying a bundle by hand

This needs only OpenSSH, which every supported distro ships:

```bash
sha256sum -c SHA256SUMS
ssh-keygen -Y verify -f /usr/lib/uun/uun/data/eol-allowed-signers \
  -I eol@uun -n uun-eol -s uun-eol-2026-12.json.sig < uun-eol-2026-12.json
```

From a source checkout, `python3 -m uun.bundle verify uun-eol-2026-12.json` also re-validates the contents. `uun --import-eol uun-eol-2026-12.json` does all of this and then makes the bundle uun's EoL data. See the README's *Offline EoL data* section.

## How a run works

1. **build** (read-only token):
   - Fetches `/api/v1/products/full` once, with a 16 MB cap.
   - Normalizes it with `uun.eol.normalize_product`.
   - Fails before anything is signed if any **quality gate** fails:
       - upstream `schema_version` isn't 1.x
       - fewer than 300 products remain, or fewer than 95% of upstream's
       - more than 5% of last month's products vanished
       - a product uun tracks vanished
   - On pull requests that touch the pipeline, the job stops here as a dry run against the live API.
2. **publish** (`main` only, `eol-signing` environment):
   - Signs with `ssh-keygen -Y sign` and verifies the signature against the key in `uun/data/eol-allowed-signers`.
   - Adds a GitHub artifact attestation and publishes the release.
   - Opens and closes a summary issue. GitHub disables scheduled workflows after 60 days without repository activity, so a monthly job that changes nothing would otherwise stop by itself.
3. **report-failure**: any failed scheduled or manual run opens an issue. Nothing is published, and uun keeps last month's data.

## One-time setup (repository owner)

Do this once, ideally on a trusted offline machine:

1. Create the signing key. CI needs it unencrypted; it's protected by the environment instead.
   ```bash
   ssh-keygen -t ed25519 -N '' -C uun-eol-signing -f uun-eol-signing
   ```
2. In **Settings → Environments**, create `eol-signing`. Under **Deployment branches**, allow only `main`. Required reviewers are optional; they make every monthly run wait for your approval.
3. Store the private key as an environment secret:
   ```bash
   gh secret set UUN_EOL_SIGNING_KEY --env eol-signing --repo nc-intezra/uun < uun-eol-signing
   ```
4. Add the public key to `uun/data/eol-allowed-signers` in a PR:
   ```bash
   printf 'eol@uun namespaces="uun-eol",valid-after="%s" %s\n' \
     "$(date -u +%Y%m%d)" "$(cat uun-eol-signing.pub)" >> uun/data/eol-allowed-signers
   ```
5. Keep an offline backup of `uun-eol-signing`, then delete the working copy.
6. After the PR merges, open **Actions → eol-data → Run workflow** with publish on to create the first bundle.

**Rotating the key:**
1. Add the new key with `valid-after`, and give the old line a `valid-before`.
2. Ship both in a uun release, then switch the secret.
3. Remove the old line in a later release.

**Until a key is configured,** `publish` stops at the signing step with a clear error, and uun refuses every bundle.
