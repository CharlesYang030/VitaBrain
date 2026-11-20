# Changelog

## v0.1.0 — 2026-11-20

- Initial public source release of the VitaBrain 8852 evaluation application.
- Includes model adapters, memory/context management, episode execution,
  audit events, trajectory metrics, interface schemas, prompts and Web UI.
- Added portable three-view image labeling so the Mock path does not require
  the original shared `scenegen` installation.
- Full scene task metadata is optional; missing metadata does not generate
  synthetic completion evidence.
- Replaced deployment-specific model profiles with a placeholder configuration.
- Added installation instructions and the MIT license.

### Validation

- Python 3.12: 99 included offline tests passed.
- Flask test-client smoke check passed for the homepage, health API, Mock run
  creation, one execution step and audit hash-chain verification.
- Validation used Mock components and did not call a paid model or launch Unity.
- Full benchmark/Unity evaluation was not performed for this source release.
