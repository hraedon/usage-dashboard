# Agent capacity integration validation

This change integrates the previously unmerged capacity API (`a64d814`) onto
wallet-enabled main (`0fe9c53`). It preserves the versioned operator API and
legacy aliases, adds the read-only agent route and CLI, and fixes timestamp
deserialization used by capacity freshness and reset assessment.

## Regression evidence

Before the timestamp correction, five new cases failed: positive and negative
offsets changed capacity after a database round trip, two offset strings failed
UTC normalization, and an elapsed reset was reported as future headroom through
the API. The corrected parser passes all five. The old Umans-invalid-account
assertion also failed after integration; Umans is now a supported provider, so
an unconfigured account correctly returns 404 and an unknown ID returns 422.

The API tests also verify wallet readings remain unknown quota capacity,
private display details are excluded, both sets of operator aliases refuse the
agent credential, and capacity GETs leave the database and scheduler untouched.

Six integration tests start a real loopback HTTP server and invoke the CLI in
subprocesses. They cover available/blocked/unknown observations, account filters,
versioned URL input, authentication and query failures, clean stdout on errors,
and refusal to refresh with a read-only credential. Fixtures are synthetic;
the test server has no provider scheduler or provider credentials.

## Checks on 2026-09-12

- Linux Python 3.12.13, locked dev and GUI extras: **752 passed**, no skips.
  SDL video and audio use dummy drivers. This matches CI and the server image's
  Python minor version.
- Ruff and strict mypy pass (32 source files).
- Windows Python 3.13.15: **129 focused tests passed**, including the real
  HTTP/CLI integration; the two additional CLI construction-error cases also
  pass in the subsequent 11-test CLI run. Ruff and strict mypy pass.
- `git diff --check` passes.
- Installed wheel from committed source
  `b7ac032931fa579888a8d53f0c2e97c9e9499b9c`: **131 focused tests passed**,
  including the six real HTTP/CLI tests. The wheel was installed into a fresh
  Python 3.12 environment with locked dependencies, outside the checkout and
  without `PYTHONPATH`. An explicit assertion confirmed the capacity module
  loaded from that environment's `site-packages`.
- Wheel SHA-256:
  `685c068a262c660f8255e0e92563770fa42fd3e54a30063808e66ecfdb7169e0`.
- Configured identifier checks pass for the tracked tree and commit messages;
  the publication owner/author check passes.

Remote CI is reported on the pull request against its current head.
These checks do not establish live provider accuracy, a deployed endpoint,
credential provisioning, Pi fleet behavior, or sufficient capacity to finish
a job. The endpoint assesses reported limits only and never reserves quota.
