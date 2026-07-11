# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Multi-directory Claude Code and Codex exports, Codex SQLite backfill, machine-readable summaries, and CLI contract tests.
- Automated frontend smoke, syntax, and repository content checks in CI.

### Changed
- Reworked the README installation path and examples to match the shipped exporter.
- Refreshed repository branding and visual assets.
- Replaced third-party web fonts with offline system font stacks.
- Consolidated shared frontend rendering helpers across the five visual variants.

## [0.1.0] - 2026-06-27

### Added
- Machine-wide OpenClaw session transcript exporter.
- Codex CLI rollout ingestion.
- OAuth provider cost flag and atomic exporter output writes.
- UI charts that include API and OAuth costs across variants, plus session grouping by `sessionId`.

### Fixed
- Corrected provider labels, UI render hardening, timestamp formatting, timezone math, subscription ROI calendar spans, and ROI proration over the actual query window.

### Changed
- Refreshed the README for the OpenClaw session analytics flow and standardized repo metadata for the Escoffier Labs org.
