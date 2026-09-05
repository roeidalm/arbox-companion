# Running the server

Two compose files, no secrets in either. Pick one:

| file | when |
| --- | --- |
| `docker-compose.yml` | just the server — Portainer, a NAS, any Docker host |
| `docker-compose.homeassistant.yml` | Home Assistant in Docker on the same host, so it can reach the server by name |

## Portainer

Stacks → **Add stack** → **Web editor** → paste the file → Deploy.

Optionally set `TZ` and `ARBOX_PORT` under *Environment variables*; both have
defaults, so a stack with nothing filled in also works.

## Command line

```bash
docker compose up -d
```

## First run

Open `http://<host>:8177`. The setup page asks for three things:

- **whitelabel** — your studio's branded app name (the name on the app you
  normally book through). Plain `Arbox` if it is not rebranded.
- **email / password** — the same ones you use in that app.

They are written to the data volume with `0600` permissions and used only to
talk to Arbox. They are never in the compose file, the image, the environment
or the logs — which is why this file is safe to paste anywhere.

The page then hands you an API key. Keep it: the browser stores it, and Home
Assistant needs it.

## Upgrades

### Local feedback rehearsal with Podman

Use the existing published image as a dependency runtime and mount local code;
this does not build or publish a release. The simulator sends Telegram and HA
HTTP requests to local mock endpoints, while callbacks use the real handlers.
It requires no account credentials and keeps its data in temporary memory.

```bash
podman run -d --name arbox-journal-local \
  -p 127.0.0.1:8178:8000 --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,size=32m \
  --tmpfs /data:rw,nosuid,mode=1777,size=32m \
  -v "$PWD/arbox-server:/work:ro" -w /work -e PYTHONPATH=/work \
  ghcr.io/roeidalm/arbox-server:1.45.0 python scripts/local_journal_demo.py
```

Run from the repository root. When using a non-default Podman machine, add
`--connection <machine-name>` immediately after `podman`.
Open `http://localhost:8178/dev`, then follow its Settings link and select
Tracking. The two test buttons deliver to the simulator page. This validates
the application flow, not actual phone delivery or the phone's layout.
Do not enter real credentials into the simulator. Restart the container after
Python changes; reload the browser after frontend changes.

### Release and managed Compose deployment

New release images are built only by `.github/workflows/release.yml`, triggered
by a new `v*` tag. Validate locally before creating that tag.

The deployed `openclaw-vm` stack is managed in `/root/apps/home-docker-stack`:

1. Update the image tag in `stacks/smart-home/arbox/docker-compose.yml` in that
   repository after the release workflow has published it.
2. Commit the desired state and run `./deploy.sh` from the stack repository root.
   It pulls Git with `--ff-only`, pulls images, and converges the root Compose
   project. Never deploy from the individual app directory or replace the
   container manually.
3. Verify `/api/health` reports the intended version and revision.

The script also installs repo-managed HA configuration and may restart HA if
that configuration changed. Check the pending stack changes before running it.
HACS integration updates remain managed by Home Assistant itself; do not copy
integration files into the running HA container.

`latest` moves on every release. To approve each one instead, pin a version
(`image: ghcr.io/roeidalm/arbox-server:1.32.1`) and, if you run Watchtower,
uncomment the label at the bottom of the file so it skips this container. The
server runs SQLite migrations on startup, which is the argument for pinning.

### Upgrading past 1.33.0 — one-time permission fix

From 1.33.0 the server runs as `nobody` (uid 65534) instead of root. A volume
created by an older version is owned by root, and the new container cannot
write to it. It does not limp along: it exits immediately with

```
PermissionError: [Errno 13] Permission denied: '/data/settings.json'
```

and, with `restart: unless-stopped`, keeps restarting into the same error.
Hand the data over once, with the container stopped:

```bash
docker run --rm -v arbox-data:/data alpine chown -R 65534:65534 /data
```

For a **bind mount** rather than a named volume, chown the host directory
instead — `chown -R 65534:65534 /your/path`. Fresh installs need neither: a
new volume inherits the ownership from the image.

Do it in this order — stop, chown, start — so the container never comes up
against data it cannot read.

## Backup

Everything worth keeping is in the `arbox-data` volume: credentials, settings,
booking history, the event log.

```bash
docker run --rm -v arbox-data:/data -v "$PWD":/out alpine \
  tar czf /out/arbox-backup.tar.gz -C /data .
```

## What the container is allowed to do

Very little, and the compose files enforce it:

| | |
| --- | --- |
| user | `nobody` (65534) — never root |
| root filesystem | read-only; `/data` is the only writable path |
| capabilities | all dropped |
| privilege escalation | blocked (`no-new-privileges`) |
| installers | pip, setuptools and wheel removed from the image |

The server reads and writes its own data directory and talks to Arbox over
HTTPS. It has no reason to touch the operating system, so it cannot.

## Exposing it

The UI is protected by an API key, not a login page. Reach it over a VPN
(Tailscale, WireGuard) or behind a reverse proxy with its own auth — not by
forwarding the port from the internet.
