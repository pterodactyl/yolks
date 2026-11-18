# Windrose

Game-specific runtime image for the native Linux Windrose dedicated server.

The image copies the application payload from the publisher-provided image:

```text
docker.io/windroseserver/windroseserver
```

and publishes the Pterodactyl-compatible runtime as:

```text
ghcr.io/pterodactyl/games:windrose
```

## Architecture

The publisher image currently provides an amd64 payload, so this image is built for `linux/amd64` only.

## Persistent paths

The application payload remains in the container image. The Pterodactyl server volume stores:

- `R5/Saved/`
- `R5/ServerDescription.json`
- `R5/GeneratedServerValues.txt`

The startup and configuration helpers are included in the image and are not editable from the server volume.

## Third-party payload

See `NOTICE.md` for the distinction between the repository's MIT-licensed compatibility files and the publisher-provided Windrose application payload.
