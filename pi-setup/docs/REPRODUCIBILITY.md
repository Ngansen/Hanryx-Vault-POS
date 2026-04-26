# Reproducible builds for the Pi's containers

This is the bump procedure for the four custom images in `pi-setup/` —
`pos` (root `Dockerfile`), `recognizer/`, `pokeapi/`, and
`services/storefront/`. Their goal is **byte-identical rebuilds**: two
`docker compose build` runs from the same git SHA produce images with
identical layer hashes.

The reproducibility guarantees layer like this:

| Layer | Locked by | Bumping it |
|---|---|---|
| Base image (`FROM`) | content-hash digest in the `FROM` line (Task #11) | edit `Dockerfile`, swap `@sha256:…` |
| `apt-get install` (Debian images) | snapshot.debian.org snapshot date | bump `APT_SNAPSHOT_DATE` build arg |
| `apk add` (Alpine pokeapi image) | `pkg=version` pins | bump `ALPINE_GIT_VERSION` / `ALPINE_BASH_VERSION` build args |
| `pip install` | `requirements.txt` with `--require-hashes` | edit `requirements.in`, regen lockfile |
| `pip install` (git+ URLs) | full commit SHA in the URL | edit `requirements-vcs.txt` |
| `npm ci` (storefront) | `package-lock.json` checked in upstream | maintain in `Ngansen/HanRyx-Vault` |

Bump anything from this table **deliberately**, never reactively. Security
updates aren't blocked — they require one explicit edit per layer (below).

---

## 1. Bumping Python deps (`pip`)

**Files:**
- `pi-setup/requirements.in` (POS, top-level deps — edit this)
- `pi-setup/requirements.txt` (POS lockfile — generated, do not edit)
- `pi-setup/requirements-vcs.txt` (POS git+ URLs — edit by hand)
- `pi-setup/recognizer/requirements.in` (recognizer — edit this)
- `pi-setup/recognizer/requirements.txt` (recognizer lockfile — generated)

### To pick up security updates / minor bumps

1. Install `uv` if you don't have it (`python3 -m pip install --user uv`,
   or `curl -LsSf https://astral.sh/uv/install.sh | sh`).
2. Edit the relevant `requirements.in` (loosen / tighten constraints as needed).
3. Regenerate the lockfile:
   ```bash
   ./pi-setup/scripts/lock-python-deps.sh pi-setup     # POS
   ./pi-setup/scripts/lock-python-deps.sh recognizer   # recognizer
   ./pi-setup/scripts/lock-python-deps.sh all          # both
   ```
   This runs `uv pip compile --generate-hashes
   --python-platform=aarch64-unknown-linux-gnu` against the Pi 5 target
   regardless of what arch you're running on — so an x86 maintainer can
   regenerate cleanly without spinning up qemu or an actual Pi.
4. Commit the regenerated `requirements.txt`. Diff should be a clean,
   reviewable set of version bumps with their sha256 hashes.
5. `docker compose build --no-cache pos recognizer` and smoke-test.

### Caveat — torch and CUDA on aarch64

PyPI's torch wheel for aarch64 declares its `nvidia-*` CUDA deps with a
`platform_system == "Linux" and platform_machine == "x86_64"` marker, so
they're correctly excluded on the Pi. **Except torch 2.11.0**, which
dropped the `platform_machine` half of that marker and now pulls
nvidia-* on aarch64 too — and those packages have no aarch64 wheels on
PyPI, so the lockfile silently breaks.

That's why `pi-setup/requirements.in` caps `torch>=2.6,<2.11` (and
`torchvision>=0.21,<0.26` to match). Bump the cap when a future torch
release fixes its aarch64 markers — verify by checking
`https://pypi.org/project/torch/<version>/` and looking for
`platform_machine == "x86_64"` on the nvidia-* lines.

### To bump the OpenAI CLIP git pin

`uv pip compile` cannot hash a `git+` URL, so CLIP lives in
`pi-setup/requirements-vcs.txt` with the **full 40-char commit SHA**.
The SHA is itself a content hash, so reproducibility is preserved.

1. Pick a new commit on https://github.com/openai/CLIP.
2. Replace the `@<sha>` in `pi-setup/requirements-vcs.txt`.
3. Rebuild and smoke-test.

---

## 2. Bumping Debian packages (`apt`)

The three Debian-based images (`pi-setup/Dockerfile`,
`pi-setup/recognizer/Dockerfile`, `pi-setup/services/storefront/Dockerfile`)
all have an `APT_SNAPSHOT_DATE` build arg pointing at
[snapshot.debian.org](https://snapshot.debian.org/). This freezes the apt
mirror to a specific point in time, so two rebuilds install identical
`.deb` files.

### To pick up security updates

1. Pick a new snapshot date in `YYYYMMDDTHHMMSSZ` format (e.g.
   `20260601T000000Z`). Verify it exists on
   `https://snapshot.debian.org/archive/debian/<DATE>/`.
2. Update `APT_SNAPSHOT_DATE` in **all three** Debian Dockerfiles
   (keep them in lock-step):
   - `pi-setup/Dockerfile` (in BOTH the builder and runtime stages)
   - `pi-setup/recognizer/Dockerfile`
   - `pi-setup/services/storefront/Dockerfile`
3. `docker compose build --no-cache` and smoke-test.

To override at build time without editing the file:

```bash
docker compose build --build-arg APT_SNAPSHOT_DATE=20260601T000000Z pos
```

---

## 3. Bumping Alpine packages (`apk`) — pokeapi only

`pi-setup/pokeapi/Dockerfile` is the only Alpine image. Alpine doesn't
have an official time-based snapshot service, so we pin the two extra
packages we install (`git`, `bash`) to specific versions.

### To find the current version inside the base image

```bash
docker run --rm nginx:1.27.2-alpine sh -c \
    'apk update -q && apk policy git bash | grep -E "^(git|bash|\s+[0-9])"'
```

### To bump

1. Update `ALPINE_GIT_VERSION` and/or `ALPINE_BASH_VERSION` build args at
   the top of `pi-setup/pokeapi/Dockerfile`.
2. Rebuild. If the version isn't available in the base image's apk repo,
   the build fails loudly with `unable to select packages: <pkg>=<ver>` —
   that's the desired behaviour (no silent drift).

---

## 4. Storefront `package-lock.json` (`npm`)

`pi-setup/services/storefront/Dockerfile` runs `npm ci`, which requires
a committed `package-lock.json`. The storefront source is cloned from
`Ngansen/HanRyx-Vault` at build time, so the lockfile lives in **that**
repo, not this one.

Two guards enforce this:

1. `pi-setup/services/storefront/build.sh` checks the lockfile exists
   right after the clone, and aborts before docker build starts.
2. The Dockerfile builder and runtime stages each `test -f
   /app/package-lock.json` before `npm ci`, so a misconfigured CI that
   skips `build.sh` still fails loudly inside the build.

### To bump npm deps

Do it in the upstream `Ngansen/HanRyx-Vault` repo:

```bash
# inside the storefront repo
npm update <pkg>
git add package.json package-lock.json
git commit -m "bump <pkg>"
git push
```

Then on the Pi (or in CI):

```bash
cd pi-setup/services/storefront
./build.sh                         # pulls fresh source incl. new lockfile
docker compose build storefront
```

---

## 5. Verifying reproducibility

To prove a rebuild is byte-identical to the previous one:

```bash
docker compose build pos recognizer storefront pokeapi
docker images --no-trunc --format '{{.Repository}}:{{.Tag}} {{.ID}}'

# Re-build from scratch and compare:
docker builder prune -af
docker compose build --no-cache pos recognizer storefront pokeapi
docker images --no-trunc --format '{{.Repository}}:{{.Tag}} {{.ID}}'
```

The `{{.ID}}` (sha256) for each image should match across the two runs.
If it doesn't, something escaped the lock — most often a layer reading
the wall-clock (timestamps in build output) rather than a real package
drift. Use `docker history --no-trunc <image>` and `dive` to find the
offending layer.
