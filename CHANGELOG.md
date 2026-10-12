# Changelog

All notable changes to Idus HW-player are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0] – 2026-10-11

First public release.

### Added
- Random Halloween video player for a Raspberry Pi + projector/TV, built on mpv.
- Shuffle without repeats: every video plays once before any repeats, and the same
  video never plays twice in a row.
- Random pitch-dark pause between videos, with a configurable shortest and longest length.
- Web GUI (PC and phone) to control everything: on/off, play now / next, stop, show
  hours, volume and mute, per-video rotation, drag-and-drop upload, and a live log.
- Password-protected web GUI with CSRF protection and a strict Content-Security-Policy.
- CLI / remote-control flags (`--status`, `--next`, `--stop`, `--on`, `--off`, `--list`)
  for KDE global shortcuts or a Stream Deck, with `--host` to control another machine.
- `install-idus-hw-player.sh`: installs mpv, sets up a self-contained app folder, and
  creates a systemd service that starts the player on every boot.
- Self-contained folder (app + icon + settings + env) so one folder backup captures
  everything except the videos.

### Notes
- Runs on a Raspberry Pi 3 / 3B as long as the videos are H.264 1080p; a Pi 4 or 5 is
  preferred (hardware H.265 decode, 4K, more headroom).
- Not affiliated with AtmosFX. Ships no videos.

[1.0]: https://github.com/Idus/idus-hw-player/releases/tag/v1.0
