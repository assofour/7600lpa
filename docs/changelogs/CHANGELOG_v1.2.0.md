# Changelog — v1.2.0

**Release date:** 2026-03-23

## Bug Fixes

- **Fix auto-enable targeting wrong profile after download** (`57b569d`)
  - After downloading a new profile, `download.py` incorrectly enabled the first (oldest) profile in the list instead of the newly installed one.
  - Root cause: `find(b"\x5A")` returned the first ICCID tag in the profile list; changed to `rfind()` to pick the last (newly appended) profile.

## Documentation

- **Rename project 7600ipa → 7600lpa** (`8619d0d`)
  - SGP.22 = consumer eSIM = LPA (Local Profile Assistant), not IPA.
- **Clean up README** (`d56bb5c`)
  - Removed vendor names, made config section optional.
- **Add tested eSIM providers** (`a675af3`)
  - Documented tested providers (Linksfield, Wireless Panda) and connectivity results.

## Tested Profiles (this release)

| Provider | SM-DP+ | Status |
|----------|--------|--------|
| Surf Mobile (go-esim) | sm-v4-064-a-gtm.pr.go-esim.com | Working (roaming, 103ms avg) |
| Sparks | — | Working (enabled) |
| JT_prod | rsp-0001.linksfield.net | Downloaded OK, no roaming coverage |
| T-Mobile | t-mobile.idemia.io | Working (home network, -67dBm) |
