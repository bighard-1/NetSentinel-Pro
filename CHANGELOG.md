# Changelog

All notable changes to NetSentinel Pro are documented here. This file follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Repository governance and release quality gates are being prepared in P5.

## [0.5.0] - 2026-11-19

### Added

- Persistent device status, probe streaks, health readiness, and bounded history retention.
- Probe retries, per-device schedules, outbox delivery retries, and protected backup restore.
- Viewer access, token revocation, notification secret encryption, and deployment hardening.
- Multiple probe targets per address, Alembic upgrades, UTC-aware timestamps, and idempotent weekly reports.
- Recycle bin, CSV import preview, device trends, manual probes, bilingual dashboard essentials, and accessibility checks.

### Fixed

- Stable targets no longer lose their current state after old alert events are cleaned up.
- Recreated targets no longer inherit an obsolete in-memory status for the same address.
- Monitoring survives transient probe or database errors and exposes a readiness signal.

### Security

- Authentication and input validation cover protected API routes; outbound notification destinations are restricted.
- Vue is served locally; the page no longer requires a third-party CDN to start.

[Unreleased]: https://github.com/bighard-1/NetSentinel-Pro/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/bighard-1/NetSentinel-Pro/releases/tag/v0.5.0
