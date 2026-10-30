# iVentoy Container

This is a docker container packaging up the iVentoy tool [https://iventoy.com](https://iventoy.com)

The image is based on Debian 13 trixie slim. iVentoy runs in foreground mode so Docker can supervise it directly and stop it cleanly.

## Versions

Docker Tag:
* latest / 1.0.49: iVentoy v1.0.49

Images are published to GitHub Container Registry: `ghcr.io/goovoz/iventoy`

Both tags point at the same multi-arch image index, but the two platforms do **not** carry the same edition:

| Platform | Upstream edition | Notes |
|---|---|---|
| `linux/amd64` | **Free** | Full feature set |
| `linux/arm64` | **Trial** | Upstream (ventoy/PXE) only publishes an arm64 *trial* build, so arm64 is not feature-equivalent to amd64 |

The final image is based on `debian:bookworm-slim` — a hard requirement, not a preference: iVentoy ships prebuilt binaries with glibc-linked libraries bundled in `lib/lin64/`, which cannot load under Alpine musl libc. The multi-stage build does download/extract in an Alpine builder stage, so the runtime image carries no build tooling or extra packages. See [docs/UPSTREAM-SYNC.md](docs/UPSTREAM-SYNC.md) for the base-image rationale.

## Usage

It uses sysvfs so the container needs to be ran in privileged mode (not ideal!) and also needs port 69 which is in a privileged range.

### DHCP

By default, I can't find a way of manipulating the configuration at first start.  Unfortunately iVentoy operates it's own DHCP server and will need to be configured to use a external DHCP server in the configuration.

For configuration of various DHCP servers see the [docs/](docs) folder.

### Ports 

The default web admin endpoint port is 26000 and can be hit using a browser pointing to http://<ip>or<localhost>:26000 in Chrome. 

The default internal iVentoy webserver for serving images is 16000 this is configurable in the admin interface documented here [https://iventoy.com/en/doc_http_url.html](https://iventoy.com/en/doc_http_url.html), if changing ensure you forward the correct port number

Port 10809 is the NBD (Network Block Device) server iVentoy uses to hand block devices to the booting client.

The complete port list, with the defaults iVentoy itself reports (`get_config_all` on the built-in web API). Everything except 26000 is configurable on the **Configuration** page — if you change one, update the port mapping too.

  - 26000/26000 TCP = iVentoy Web GUI (fixed, not configurable)
  - 16000/16000 TCP = iVentoy ISO & file server (`http_port`)
  - 10809/10809 TCP = NBD block device (`nbd_port`)
  - 3260/3260 TCP = iSCSI (`iscsi_port`)
  - 10445/10445 TCP = SMB (`smb_port`)
  - 12049/12049 TCP = NFS (`nfs_port`)
  - 69/69 UDP = TFTP
  - 67/67 UDP = built-in DHCP server (ProxyNet / External mode)
  - 4011/4011 UDP = proxyDHCP (Proxy / ProxyNet mode)

67 and 4011 rely on layer-2 broadcast, so they only work with `network_mode: host` — bridge networking cannot deliver DHCP broadcasts to the container. Every other port works fine with plain port mapping.

## Volumes

There are a couple of volumes you can mount, the primary is the `iso` folder, which surprisingly containts your iso images you want to boot.

```
docker run -d --privileged -p 69:69/udp -p 26000:26000 -p 16000:16000 -p 10809:10809 -p 3260:3260 -p 10445:10445 -p 12049:12049 -v /path/to/iso:/iventoy/iso ghcr.io/goovoz/iventoy:latest
```

### Persisting Configuration

Following the official upgrade rules, the container refreshes the version-specific `iventoy.dat` and `mac.db` files from the image on every start. Only the generated `config.dat` settings and mounted ISO files are carried across releases. You can therefore mount the persistent data directory on the first run:

```
docker run -d --privileged -p 69:69/udp -p 26000:26000 -p 16000:16000 -p 10809:10809 -p 3260:3260 -p 10445:10445 -p 12049:12049 -v /path/to/isos:/iventoy/iso -v /path/to/data:/iventoy/data --name iventoy ghcr.io/goovoz/iventoy:latest
```

Automatic PXE startup is disabled by default so the admin service remains available when the recovered Server IP is missing or no longer exists on the host. Configure a valid Server IP in the web UI first. To opt in afterward, set `IVENTOY_AUTO_RUN=1`; other values, including `0`, keep automatic startup disabled.

Alternatively run on host mode to serve PXE to docker host's LAN:

```
docker run -d --privileged --net=host -v /path/to/isos:/iventoy/iso -v /path/to/data:/iventoy/data --name iventoy ghcr.io/goovoz/iventoy:latest
```

### Other mountable directories

Besides `data` and `iso`, these directories can be mounted when you use the matching iVentoy features:

| Host path | Container path | Contents |
|---|---|---|
| `/path/to/driver` | `/iventoy/driver` | Driver ISO used by the *driver mode* / Windows driver injection |
| `/path/to/user` | `/iventoy/user` | Your own files: auto-install scripts, third-party tools |

Do **not** mount `/iventoy/log`: the image symlinks `log/log.txt` to the container's stdout, which is what makes `docker logs` (and your container manager) show the iVentoy log. Mounting a host directory over it hides the symlink and the log silently goes to the file only.

### Docker Compose

A ready-to-use [docker-compose.yml](docker-compose.yml) is included (image pinned to `:latest`, all ports, `data` / `iso` / `driver` / `user` volumes, `TZ`). Adjust the host paths and run:

```
docker compose up -d
```

The compose file ships three operational defaults that matter in practice:

- `init: true` — iVentoy forks child processes; without an init as PID 1 nobody reaps them.
- `stop_grace_period: 30s` — iVentoy flushes `config.dat` on shutdown, and plain `docker stop` would SIGKILL it after 10s.
- `logging` with `max-size` / `max-file` — the log is symlinked to stdout, so it lands in docker's json-file driver; without a cap it grows until the disk is full.

### Health check

The image's `HEALTHCHECK` only proves the **management** service is answering on TCP 26000: it reads the `LISTEN` state of port 26000 out of `/proc/net/tcp{,6}` (the runtime image carries no `curl`/`wget`). It does **not** mean the PXE service is running — that still depends on `IVENTOY_AUTO_RUN=1` and a valid Server IP.

### Configure your DHCP server
See the [docs](docs/) folder for examples.

### Configure iVentoy

Once your container is up and running go to the IP address of your server on port 26000 e.g. http://10.0.0.1:26000

1. Ensure the Server IP is selected correctly, on my docker setup I have a dedicated network with it's own IP for this service.
![picture of iVentoy configuration 1](docs/assets/scr1.png)

2. On the configuration menu on the left, If you are using a external DHCP server then ensure the `DHCP Server Mode` is set to `External`
![picture of iVentoy configuration 2](docs/assets/scr2.png)

3. Copy a ISO file to your isos folder (locally as set by the volumes above) and goito the `Image Management` menu on the left and hit refresh.
![picture of iVentoy configuration 2](docs/assets/scr3.png)
![picture of iVentoy configuration 2](docs/assets/scr4.png)

4. Start the server by hitting the bit play button on the `Boot Information` menu screen
![picture of iVentoy configuration 1](docs/assets/scr1.png)
![picture of iVentoy configuration 1](docs/assets/scr5.png)

5. Test booting.
![picture of iVentoy configuration 1](docs/assets/scr6.png)


## Automatic Version Tracking

This fork follows the official iVentoy releases from [ventoy/PXE](https://github.com/ventoy/PXE/releases) automatically:

- A scheduled GitHub Actions workflow (`.github/workflows/track-iventoy-release.yml`) checks the upstream release feed every day.
- When a new stable version appears, it bumps the version references in `Dockerfile` and `README.md`, pushes the commit to `main`, creates a `vX.Y.Z` tag, triggers the multi-arch image build, and only reports success once the image is actually published. (`docker-compose.yml` uses `:latest` and `Makefile` reads the version out of the `Dockerfile`, so neither carries a version string of its own.)
- If the upstream version matches the version in this repository, nothing is pushed — but the workflow still **reconciles the registry**: if `ghcr.io/goovoz/iventoy:<version>` is missing (a previous build failed, the dispatch was rejected, or the tag was pruned), it re-dispatches the build and fails loudly instead of leaving the image silently stuck on an older release.

You can also run it manually: **Actions → "Track iVentoy Upstream Release" → Run workflow** (optionally pinning an explicit version). See [docs/UPSTREAM-SYNC.md](docs/UPSTREAM-SYNC.md) for the full mechanism.
