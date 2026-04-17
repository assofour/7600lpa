# Changelog — v1.2.1

**Release date:** 2026-04-17

## Bug Fixes

- **Correctly identify the newly installed profile for auto-enable**
  - The previous approaches (first `find(b"\x5A")` and later `rfind()`) were both brittle byte scans against the `BF2D` profile list response:
    - `0x5A` is a legitimate byte value that can appear inside unrelated TLV fields (e.g. inside `4F` AID bytes or within text fields), so scanning for it can land on non-ICCID data.
    - The ordering of entries in the profile list is not defined by SGP.22; some eUICC implementations return insertion order (oldest first), meaning neither the first nor last `5A` reliably points to the just-installed profile.
  - Symptom observed when downloading two profiles in sequence: the first download's auto-enable targeted a pre-existing profile by mistake, leaving the newly installed profile disabled. The second download then failed with `enableResult=2` (`profileNotInDisabledState`) because the pre-existing profile was already enabled.
  - **Correct fix:** parse the installed profile ICCID directly from the `ProfileInstallationResult` returned by `LoadBoundProfilePackage`, following the nested TLV path `BF37 → BF27 → BF2F → 5A` (`notificationMetadata.iccid`).
  - Verified against three real PIR responses from different SM-DP+ servers — all decode to the correct newly-installed ICCID.
