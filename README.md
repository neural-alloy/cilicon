<div align="center">

<img src="assets/neural-alloy.png" alt="Neural Alloy" width="140" />

# cilicon

**CI for real hardware** — build _and_ **boot** your code on every chip you ship to, in parallel, owning zero hardware.

[**Get started**](https://github.com/apps/cilicon-action/installations/new)&nbsp;&nbsp;·&nbsp;&nbsp;[Docs](https://neuralalloy.io/cilicon/docs.html)&nbsp;&nbsp;·&nbsp;&nbsp;[Neural Alloy](https://neuralalloy.io)

</div>

---

Regular CI says it **compiled.** cilicon says it **boots on the chip.**

Cross-build every target in your `cilicon.yml`, **boot the artifact** in a cloud emulator (or a real GPU), all in parallel — then one PR check, boot-proven and signed. A step in your CI, not a new one.

```yaml
# cilicon.yml
targets:
  - id: firmware/stm32
    build: make
    validate: qemu_system      # boot it in QEMU
    machine: lm3s6965evb
    expect: "BOOT OK"          # the proof it booted
    flash_max: 256K            # …and fits the silicon
```

## Get started

**The app — zero setup, no token.** [Install it](https://github.com/apps/cilicon-action/installations/new) on a repo, drop a `cilicon.yml`, push. Every PR gets a boot-tested check, run on our cloud.

**Self-host** on your own [Modal](https://modal.com):

```yaml
# .github/workflows/cilicon.yml
- uses: neural-alloy/cilicon@v1
  env:
    MODAL_TOKEN_ID:     ${{ secrets.MODAL_TOKEN_ID }}
    MODAL_TOKEN_SECRET: ${{ secrets.MODAL_TOKEN_SECRET }}
```

**Local**, nothing to install:

```bash
uvx --from git+https://github.com/neural-alloy/cilicon@v1 cilicon run
```

## A green check is a receipt

- **Boot proof** — the `expect` string has to land in the real console. It ran, it didn't just link.
- **Size gates** — `flash_max` / `ram_max` fail anything that won't fit the silicon.
- **Signed** — `--attestation` emits an Ed25519 DSSE boot proof. Verify it anywhere.
- **Fleet-ready** — register the proof to your [Neural Alloy](https://neuralalloy.io) fleet.

Boots MCUs (QEMU), full Linux (ARM64), Renode, and real GPUs — 100+ boards as one-word presets. **[Full docs →](https://neuralalloy.io/cilicon/docs.html)**

---

<div align="center">
<sub>A <a href="https://neuralalloy.io"><b>Neural Alloy</b></a> product · cilicon is the free way in · MIT © Ryan Rana</sub>
</div>
