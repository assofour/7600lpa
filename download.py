#!/usr/bin/env python3
"""
download.py — SGP.22 Profile Download (ES9+ ↔ ES10b)

Implements the full GSMA SGP.22 profile download sequence:

  ES10b (eUICC APDU)          ES9+ (HTTPS to SM-DP+)
  ──────────────────          ──────────────────────
  GetEUICCChallenge    ──→
  GetEUICCInfo1        ──→
                              initiateAuthentication  →  serverSigned1 + cert
  AuthenticateServer   ←──────────────────────────────
                              authenticateClient      →  profileMetadata
  PrepareDownload      ←──────────────────────────────
                              getBoundProfilePackage  →  BPP (encrypted profile)
  LoadBoundProfilePackage (STORE DATA chunks)
  EnableProfile

Usage:
    sudo python3 download.py [options]
    sudo python3 download.py --smdp smdp.example.com --mid YOUR-MATCHING-ID
    sudo python3 download.py --config config.yaml
"""

import argparse
import base64
import json
import logging
import struct
import sys
import time
from pathlib import Path

import yaml

try:
    import requests
except ImportError:
    sys.exit("ERROR: requests not installed — run: pip install requests")

from transport import RealTransport, MockTransport, QmiTransport
from lpa_manager import LPAManager, _parse_tlv, _find_tag

logger = logging.getLogger("download")


# ─────────────────────────────────────────────────────────────────────────────
# BER-TLV encoder
# ─────────────────────────────────────────────────────────────────────────────

def _tlv(tag: bytes, value: bytes) -> bytes:
    n = len(value)
    if n < 0x80:
        length = bytes([n])
    elif n < 0x100:
        length = bytes([0x81, n])
    elif n < 0x10000:
        length = bytes([0x82, n >> 8, n & 0xFF])
    else:
        raise ValueError(f"TLV value too large: {n} bytes")
    return tag + length + value


def _encode_apdu(cla: int, ins: int, p1: int, p2: int,
                 data: bytes, with_le: bool = True) -> bytes:
    """Build an APDU: CLA INS P1 P2 [Lc data] [Le].
    with_le=True  → case 4 (command + response expected)
    with_le=False → case 3 (command only, no response expected)
    """
    n = len(data)
    if n == 0:
        return bytes([cla, ins, p1, p2, 0x00])
    header = bytes([cla, ins, p1, p2])
    if n <= 255:
        result = header + bytes([n]) + data
        return result + b"\x00" if with_le else result
    # Extended length (3-byte Lc)
    result = header + bytes([0x00, n >> 8, n & 0xFF]) + data
    return result + b"\x00\x00" if with_le else result


def _ctx_tlv(tag: int, value: bytes) -> bytes:
    """BER-TLV with a single-byte context-specific tag."""
    n = len(value)
    if n < 0x80:
        length = bytes([n])
    elif n < 0x100:
        length = bytes([0x81, n])
    else:
        length = bytes([0x82, n >> 8, n & 0xFF])
    return bytes([tag]) + length + value


def build_ctx_params1(matching_id: str = "") -> bytes:
    """
    Build CtxParams1 (tag A0) for AuthenticateServerRequest.

    Structure (per SGP.22 + lpac reference):
      A0 {                             -- ctxParams1
        [80 <matchingId UTF8>]         -- optional
        A1 {                           -- deviceInfo
          80 04 <TAC 4 bytes>          -- tac (default 35290611)
          A1 00                        -- deviceCapabilities (empty)
        }
      }
    """
    # deviceInfo children
    tac = _ctx_tlv(0x80, bytes([0x35, 0x29, 0x06, 0x11]))  # default TAC
    dev_caps = _ctx_tlv(0xA1, b"")                           # empty deviceCapabilities
    device_info = _ctx_tlv(0xA1, tac + dev_caps)

    inner = b""
    if matching_id:
        inner += _ctx_tlv(0x80, matching_id.encode("utf-8"))
    inner += device_info

    return _ctx_tlv(0xA0, inner)


def _parse_ber_tl(data: bytes, pos: int):
    """Parse a BER-TLV tag + length starting at pos.
    Returns (tag_bytes, value_start, value_len) or raises ValueError.
    """
    start = pos
    if pos >= len(data):
        raise ValueError("No data for TLV tag")
    first = data[pos]; pos += 1
    # Multi-byte tag: if low 5 bits are all 1
    if (first & 0x1F) == 0x1F:
        while pos < len(data) and (data[pos] & 0x80):
            pos += 1
        if pos < len(data):
            pos += 1  # final tag byte
    tag_bytes = data[start:pos]

    # Parse length
    if pos >= len(data):
        raise ValueError("No data for TLV length")
    lb = data[pos]; pos += 1
    if lb < 0x80:
        vlen = lb
    elif lb == 0x81:
        vlen = data[pos]; pos += 1
    elif lb == 0x82:
        vlen = (data[pos] << 8) | data[pos + 1]; pos += 2
    elif lb == 0x83:
        vlen = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]; pos += 3
    else:
        raise ValueError(f"Unsupported length byte 0x{lb:02X}")
    return tag_bytes, pos, vlen


def _iter_ber_tlv(data: bytes):
    """Iterate over consecutive BER-TLV objects. Yields full TLV bytes."""
    pos = 0
    while pos < len(data):
        tag_bytes, val_start, val_len = _parse_ber_tl(data, pos)
        end = val_start + val_len
        if end > len(data):
            logger.warning("TLV at offset %d: tag=%s val_len=%d exceeds data (%d)",
                           pos, tag_bytes.hex().upper(), val_len, len(data))
            break
        yield data[pos:end]
        pos = end


def _is_tagged(data: bytes, expected_tag: bytes) -> bool:
    """Check if data is already a well-formed BER-TLV with the expected tag.
    Validates that tag matches AND the encoded length equals the remaining data.
    """
    tag_len = len(expected_tag)
    if len(data) < tag_len + 1:
        return False
    if data[:tag_len] != expected_tag:
        return False
    pos = tag_len
    first = data[pos]
    if first < 0x80:
        value_len = first
        header_len = tag_len + 1
    elif first == 0x81:
        if pos + 1 >= len(data):
            return False
        value_len = data[pos + 1]
        header_len = tag_len + 2
    elif first == 0x82:
        if pos + 2 >= len(data):
            return False
        value_len = (data[pos + 1] << 8) | data[pos + 2]
        header_len = tag_len + 3
    else:
        return False
    return header_len + value_len == len(data)


# ─────────────────────────────────────────────────────────────────────────────
# ES10b — eUICC APDU commands
# ─────────────────────────────────────────────────────────────────────────────

class ES10b:
    """
    SGP.22 ES10b interface — APDUs sent to the ISD-R via the LPA transport.

    Uses STORE DATA (INS=E2) wrapping for ALL commands to bypass the
    SIM7600G-H modem firmware APDU filter which blocks proprietary
    instructions (INS=BA, INS=88, etc.) but allows INS=E2.

    Each ES10b command is sent as:
      CLA=0x80  INS=0xE2  P1=0x91  P2=0x00  Lc  <BF?? TLV>  Le=0x00
    """

    def __init__(self, lpa: LPAManager):
        self.lpa = lpa

    def _send(self, apdu: bytes):
        resp = self.lpa.transport.send_apdu(apdu)
        logger.debug("ES10b APDU << data=%s SW=%s",
                     resp.data.hex().upper() if resp.data else "(none)",
                     resp.sw_hex)
        return resp

    def _store_data(self, data: bytes) -> bytes:
        """
        Send an ES10b command wrapped in STORE DATA (INS=E2, P1=0x91).
        Returns the response data bytes.
        Raises RuntimeError on non-9000 status.
        """
        apdu = _encode_apdu(0x80, 0xE2, 0x91, 0x00, data)
        resp = self._send(apdu)
        if not resp.success:
            raise RuntimeError(
                f"STORE DATA failed: SW={resp.sw_hex} "
                f"(data tag={data[:2].hex().upper() if len(data) >= 2 else '?'})")
        return resp.data

    def _store_data_chunked(self, data: bytes, chunk_size: int = 200) -> bytes:
        """
        Send large ES10b payloads via multi-block STORE DATA.
        P1=0x11 for intermediate blocks, P1=0x91 for the last block.
        P2 increments as block counter.
        Intermediate blocks use case-3 APDU (no Le); last block uses case-4 (Le=00).
        Returns the response data from the final block.
        """
        chunks = [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)]
        logger.debug("STORE DATA chunked: %d bytes → %d chunks", len(data), len(chunks))
        resp = None
        for idx, chunk in enumerate(chunks):
            last = (idx == len(chunks) - 1)
            p1 = 0x91 if last else 0x11
            p2 = idx & 0xFF
            apdu = _encode_apdu(0x80, 0xE2, p1, p2, chunk, with_le=last)
            resp = self._send(apdu)
            if not resp.success:
                raise RuntimeError(
                    f"STORE DATA chunk {idx+1}/{len(chunks)} failed: SW={resp.sw_hex}")
        return resp.data if resp else b""

    # ── ES10b.GetEUICCChallenge ───────────────────────────────────────────────

    def get_euicc_challenge(self) -> bytes:
        """Returns 16-byte random challenge from eUICC (tag BF2E→80)."""
        # ES10c-style: STORE DATA with BF2E (empty body)
        request = _tlv(b"\xBF\x2E", b"")
        data = self._store_data(request)

        try:
            challenge = _find_tag(data, 0xBF2E, 0x80)
        except KeyError:
            try:
                challenge = _find_tag(data, 0xBF2E, 0x5C)
            except KeyError:
                challenge = data
        if len(challenge) != 16:
            raise RuntimeError(f"Expected 16-byte challenge, got {len(challenge)}: "
                               f"{challenge.hex().upper()}")
        logger.info("eUICC challenge: %s", challenge.hex().upper())
        return challenge

    # ── ES10b.GetEUICCInfo1 ───────────────────────────────────────────────────

    def get_euicc_info1(self) -> bytes:
        """
        Returns raw BF20 response bytes (eUICCInfo1) via STORE DATA.
        Falls back to empty bytes on failure.
        """
        request = _tlv(b"\xBF\x20", b"")
        try:
            data = self._store_data(request)
        except RuntimeError as exc:
            logger.warning("GetEUICCInfo1 failed: %s — sending empty euiccInfo1", exc)
            return b""
        logger.debug("eUICCInfo1 (%d bytes): %s", len(data), data.hex().upper())
        return data

    # ── ES10b.AuthenticateServer ──────────────────────────────────────────────

    def authenticate_server(self, server_signed1: bytes,
                            server_sig1: bytes,
                            ci_pk_id: bytes,
                            server_cert: bytes,
                            ctx_params1: bytes = b"") -> bytes:
        """
        Sends BF38 (AuthenticateServerRequest) via STORE DATA.
        Returns raw response bytes (BF38 response from eUICC).

        The ES9+ JSON response returns base64-encoded DER values that
        may already include their BER-TLV tags.  We auto-detect to avoid
        double-wrapping (which corrupts the structure and causes
        undefinedError from the eUICC).
        """
        # serverSignature1 — expect tag 5F37 (APPLICATION 55)
        if _is_tagged(server_sig1, b"\x5F\x37"):
            sig1_der = server_sig1
            logger.debug("serverSignature1: already 5F37-tagged (%d B)", len(sig1_der))
        else:
            sig1_der = _tlv(b"\x5F\x37", server_sig1)
            logger.debug("serverSignature1: wrapped in 5F37 (%d→%d B)",
                         len(server_sig1), len(sig1_der))

        # euiccCiPKIdToBeUsed — expect tag 04 (OCTET STRING)
        if _is_tagged(ci_pk_id, b"\x04"):
            ci_der = ci_pk_id
            logger.debug("euiccCiPKIdToBeUsed: already 04-tagged (%d B)", len(ci_der))
        else:
            ci_der = _tlv(b"\x04", ci_pk_id)
            logger.debug("euiccCiPKIdToBeUsed: wrapped in 04 (%d→%d B)",
                         len(ci_pk_id), len(ci_der))

        inner = server_signed1 + sig1_der + ci_der + server_cert
        if ctx_params1:
            inner += ctx_params1
        bf38 = _tlv(b"\xBF\x38", inner)

        logger.info("AuthenticateServer BF38: %d bytes, %d chunks",
                    len(bf38), (len(bf38) + 199) // 200)
        logger.debug("BF38 hex head: %s…", bf38[:60].hex().upper())
        data = self._store_data_chunked(bf38)
        return data

    # ── ES10b.PrepareDownload ─────────────────────────────────────────────────

    def prepare_download(self, smdp_signed2: bytes,
                         smdp_sig2: bytes,
                         hash_cc: bytes = b"",
                         smdp_cert: bytes = b"") -> bytes:
        """
        Sends BF21 (PrepareDownloadRequest) via STORE DATA.
        Returns raw response bytes (BF21 response from eUICC).
        """
        # smdpSignature2 — expect tag 5F37 (APPLICATION 55)
        if _is_tagged(smdp_sig2, b"\x5F\x37"):
            sig2_der = smdp_sig2
        else:
            sig2_der = _tlv(b"\x5F\x37", smdp_sig2)

        inner = smdp_signed2 + sig2_der
        if hash_cc:
            # hashCc is already DER-encoded from the server response
            inner += hash_cc
        if smdp_cert:
            inner += smdp_cert
        bf21 = _tlv(b"\xBF\x21", inner)

        logger.info("PrepareDownload BF21: %d bytes, %d chunks",
                    len(bf21), (len(bf21) + 199) // 200)
        data = self._store_data_chunked(bf21)
        return data

    # ── ES10b.LoadBoundProfilePackage ─────────────────────────────────────────

    def load_bound_profile_package(self, bpp: bytes) -> bool:
        """
        Installs the Bound Profile Package via STORE DATA (INS=E2).

        Per lpac reference: the BPP is NOT sent as one continuous stream.
        Instead it is segmented into separate STORE DATA sessions:
          1. BF36 header + BF23 (InitialiseSecureChannel)
          2. A0 (ConfigureISDP)
          3. A1 header only (tag+length, no value)
          4. Each child of A1 individually
          5. A2 (ReplaceSessionKeys) — if present
          6. A3 header only (tag+length, no value)
          7. Each child of A3 individually
        Each segment is sent via _store_data_chunked (own P2 counter).
        """
        # ── Parse BF36 outer wrapper ──────────────────────────────────────────
        tag_bytes, val_start, val_len = _parse_ber_tl(bpp, 0)
        if tag_bytes != b"\xBF\x36":
            raise RuntimeError(f"BPP does not start with BF36 (got {tag_bytes.hex().upper()})")
        bf36_header = bpp[:val_start]   # BF 36 + length encoding
        inner = bpp[val_start:val_start + val_len]
        logger.info("BPP: %d bytes, BF36 header %d bytes, inner %d bytes",
                     len(bpp), len(bf36_header), len(inner))

        # ── Parse inner TLVs ─────────────────────────────────────────────────
        segments = []   # list of (tag_hex, full_tlv_bytes)
        pos = 0
        while pos < len(inner):
            tb, vs, vl = _parse_ber_tl(inner, pos)
            end = vs + vl
            tag_hex = tb.hex().upper()
            full_tlv = inner[pos:end]
            header_only = inner[pos:vs]     # tag + length bytes (no value)
            segments.append((tag_hex, full_tlv, header_only, inner[vs:end]))
            pos = end
        logger.info("BPP segments: %s",
                     ", ".join(f"{t}({len(f)}B)" for t, f, _, _ in segments))

        seg_idx = 0
        last_resp = b""

        def send_segment(label: str, data: bytes):
            nonlocal seg_idx, last_resp
            seg_idx += 1
            logger.info("BPP seg %d: %s (%d bytes)", seg_idx, label, len(data))
            last_resp = self._store_data_chunked(data, chunk_size=120)

        # ── 1. BF36 header + BF23 (InitialiseSecureChannel) ──────────────────
        bf23 = None
        remaining = []
        for tag_hex, full_tlv, header_only, value_bytes in segments:
            if tag_hex == "BF23" and bf23 is None:
                bf23 = full_tlv
            else:
                remaining.append((tag_hex, full_tlv, header_only, value_bytes))

        if bf23 is None:
            raise RuntimeError("BPP missing BF23 (InitialiseSecureChannel)")

        send_segment("BF36+BF23", bf36_header + bf23)

        # ── 2..N. Remaining segments ─────────────────────────────────────────
        for tag_hex, full_tlv, header_only, value_bytes in remaining:
            if tag_hex in ("A1", "A3"):
                # Container tags: send header only, then each child
                send_segment(f"{tag_hex} header", header_only)
                for child_tlv in _iter_ber_tlv(value_bytes):
                    child_tag, _, _ = _parse_ber_tl(child_tlv, 0)
                    send_segment(f"{tag_hex}/{child_tag.hex().upper()}",
                                 child_tlv)
            else:
                # Leaf tags (A0, A2, etc.): send full TLV
                send_segment(tag_hex, full_tlv)

        logger.info("BPP loading complete: %d segments sent", seg_idx)
        if last_resp:
            logger.info("BPP last segment response: %d bytes: %s",
                        len(last_resp), last_resp.hex().upper()[:200])
        return last_resp

    # ── ES10b.EnableProfile ───────────────────────────────────────────────────

    def _profile_id_tlv(self, iccid: bytes = b"", aid: bytes = b"") -> bytes:
        """Build profileIdentifier with EXPLICIT [0] (A0) wrapper.

        SGP.22 AUTOMATIC TAGS: CHOICE in SEQUENCE → EXPLICIT [0].
          profileIdentifier [0] CHOICE {       -- tag A0
            isdpAid [APPLICATION 15] OCTET STRING,  -- tag 4F
            iccid   [APPLICATION 26] OCTET STRING   -- tag 5A
          }
        """
        if aid:
            return _tlv(b"\xA0", _tlv(b"\x4F", aid))
        return _tlv(b"\xA0", _tlv(b"\x5A", iccid))

    def enable_profile(self, iccid: bytes = b"",
                       refresh: bool = True) -> bool:
        """
        Enable an installed profile (BF31) via STORE DATA.
        If iccid is empty, enables the most recently installed profile.
        refresh=True sends refreshFlag=1 (card should trigger REFRESH).
        """
        inner = self._profile_id_tlv(iccid=iccid)
        if refresh:
            inner += _tlv(b"\x81", b"\xFF")    # refreshFlag = TRUE (DER canonical)
        bf31 = _tlv(b"\xBF\x31", inner)
        try:
            resp = self._store_data(bf31)
            logger.info("EnableProfile response: %s (%d bytes)",
                        resp.hex().upper() if resp else "(empty)", len(resp) if resp else 0)
            return True
        except RuntimeError as exc:
            logger.warning("EnableProfile failed: %s", exc)
            return False

    # ── ES10c.DisableProfile ──────────────────────────────────────────────────

    def disable_profile(self, iccid: bytes = b"",
                        refresh: bool = True) -> bool:
        """Disable an enabled profile (BF32) via STORE DATA.
        Same A0-wrapped encoding as EnableProfile."""
        inner = self._profile_id_tlv(iccid=iccid)
        if refresh:
            inner += _tlv(b"\x81", b"\xFF")
        bf32 = _tlv(b"\xBF\x32", inner)
        try:
            resp = self._store_data(bf32)
            logger.info("DisableProfile response: %s (%d bytes)",
                        resp.hex().upper() if resp else "(empty)", len(resp) if resp else 0)
            return True
        except RuntimeError as exc:
            logger.warning("DisableProfile failed: %s", exc)
            return False

    # ── ES10c.DeleteProfile ───────────────────────────────────────────────────

    def delete_profile(self, iccid: bytes = b"") -> bool:
        """Delete a disabled profile (BF33) via STORE DATA.
        Note: BF33 does NOT use A0 wrapper (different ASN.1 structure)."""
        if iccid:
            inner = _tlv(b"\x5A", iccid)
        else:
            inner = _tlv(b"\x5A", b"")
        bf33 = _tlv(b"\xBF\x33", inner)
        try:
            resp = self._store_data(bf33)
            logger.info("DeleteProfile response: %s (%d bytes)",
                        resp.hex().upper() if resp else "(empty)", len(resp) if resp else 0)
            return True
        except RuntimeError as exc:
            logger.warning("DeleteProfile failed: %s", exc)
            return False

    # ── ES10c.ListProfiles ────────────────────────────────────────────────────

    def list_profiles(self) -> list:
        """List installed profiles (BF2D). Returns list of dicts."""
        request = _tlv(b"\xBF\x2D", b"")
        try:
            data = self._store_data(request)
        except RuntimeError as exc:
            logger.warning("ListProfiles failed: %s", exc)
            return []

        profiles = []
        if not data or len(data) < 4:
            return profiles

        # Parse BF2D { A0 { E3 { ... } E3 { ... } } }
        try:
            _, vs, vl = _parse_ber_tl(data, 0)  # BF2D
            inner = data[vs:vs + vl]
            if inner and inner[0] == 0xA0:
                _, avs, avl = _parse_ber_tl(inner, 0)
                inner = inner[avs:avs + avl]
            pos = 0
            while pos < len(inner):
                if inner[pos] != 0xE3:
                    break
                _, evs, evl = _parse_ber_tl(inner, pos)
                entry = inner[evs:evs + evl]
                profile = {}
                epos = 0
                while epos < len(entry):
                    tb, tvs, tvl = _parse_ber_tl(entry, epos)
                    val = entry[tvs:tvs + tvl]
                    tag_hex = tb.hex().upper()
                    if tag_hex == "5A":
                        profile["iccid_raw"] = val.hex().upper()
                        # Decode BCD-swapped ICCID
                        decoded = ""
                        for b in val:
                            decoded += f"{b & 0x0F:X}{(b >> 4) & 0x0F:X}"
                        profile["iccid"] = decoded.rstrip("F")
                    elif tag_hex == "4F":
                        profile["aid"] = val.hex().upper()
                    elif tag_hex == "9F70":
                        profile["state"] = "enabled" if val[0] == 1 else "disabled"
                    elif tag_hex == "91":
                        profile["name"] = val.decode("utf-8", errors="replace")
                    elif tag_hex == "92":
                        profile["provider"] = val.decode("utf-8", errors="replace")
                    elif tag_hex == "95":
                        profile["class"] = {0: "test", 1: "provisioning", 2: "operational"}.get(val[0], str(val[0]))
                    epos = tvs + tvl
                profiles.append(profile)
                pos = evs + evl
        except (ValueError, IndexError) as exc:
            logger.warning("Profile list parse error: %s", exc)
        return profiles


# ─────────────────────────────────────────────────────────────────────────────
# ES9+ — SM-DP+ HTTP client
# ─────────────────────────────────────────────────────────────────────────────

_ES9_HEADER = "gsma/rsp/v2.5.0"


class SmDpClient:
    """
    GSMA SGP.22 ES9+ HTTP client.
    Talks to SM-DP+ at https://<address>/gsma/rsp2/es9plus/
    """

    def __init__(self, address: str, matching_id: str, verify_ssl: bool = True):
        self.address = address
        self.matching_id = matching_id
        self.base = f"https://{address}/gsma/rsp2/es9plus"
        self.verify = verify_ssl
        self.transaction_id: str = ""
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "X-Admin-Protocol": _ES9_HEADER,
            "User-Agent": "lpac/python/1.0",
        })

    def _post(self, path: str, body: dict) -> dict:
        url = f"{self.base}/{path}"
        logger.debug("ES9+ POST %s  body=%s", url, json.dumps(body)[:200])
        resp = self._session.post(url, json=body, verify=self.verify, timeout=30)
        logger.debug("ES9+ ← HTTP %d  body=%s", resp.status_code, resp.text[:300])

        if resp.status_code != 200:
            raise RuntimeError(
                f"ES9+ {path}: HTTP {resp.status_code}\n{resp.text[:500]}")

        if not resp.text.strip():
            raise RuntimeError(
                f"ES9+ {path}: empty response body — "
                "server may have rejected the payload silently")

        data = resp.json()

        # Check for SGP.22 function execution status error
        status = (data.get("header", {})
                  .get("functionExecutionStatus", {})
                  .get("status", ""))
        if status == "Failed":
            sc = (data["header"]["functionExecutionStatus"]
                  .get("statusCodeData", {}))
            raise RuntimeError(
                f"ES9+ {path} failed: {sc.get('subjectCode')} / "
                f"{sc.get('reasonCode')} — {sc.get('message', '')}")

        return data

    # ── Step 1 ────────────────────────────────────────────────────────────────

    def initiate_authentication(self, euicc_challenge: bytes,
                                euicc_info1: bytes) -> dict:
        """
        POST initiateAuthentication
        Returns dict with serverSigned1, serverSignature1,
        euiccCiPKIdToBeUsed, serverCertificate.
        """
        body = {
            "euiccChallenge": base64.b64encode(euicc_challenge).decode(),
            "euiccInfo1":     base64.b64encode(euicc_info1).decode() if euicc_info1 else "",
            "smdpAddress":    self.address,
        }
        data = self._post("initiateAuthentication", body)
        self.transaction_id = data.get("transactionId", "")
        logger.info("transactionId: %s", self.transaction_id)
        return data

    # ── Step 2 ────────────────────────────────────────────────────────────────

    def authenticate_client(self, euicc_auth_response: bytes,
                            eid: str = "") -> dict:
        """
        POST authenticateClient
        Returns dict with profileMetaData, smdpSigned2, smdpSignature2, [hashCc].
        """
        body = {
            "transactionId":            self.transaction_id,
            "authenticateServerResponse": base64.b64encode(euicc_auth_response).decode(),
            "matchingId":               self.matching_id,
        }
        if eid:
            body["eid"] = eid
        data = self._post("authenticateClient", body)
        return data

    # ── Step 3 ────────────────────────────────────────────────────────────────

    def get_bound_profile_package(self, prepare_download_response: bytes) -> bytes:
        """
        POST getBoundProfilePackage
        Returns raw BPP bytes (decoded from base64).
        """
        body = {
            "transactionId":         self.transaction_id,
            "prepareDownloadResponse": base64.b64encode(
                prepare_download_response).decode(),
        }
        data = self._post("getBoundProfilePackage", body)

        bpp_b64 = data.get("boundProfilePackage", "")
        if not bpp_b64:
            raise RuntimeError("getBoundProfilePackage response missing "
                               "'boundProfilePackage' field")
        bpp = base64.b64decode(bpp_b64)
        logger.info("Bound Profile Package received: %d bytes", len(bpp))
        return bpp


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def run_download(transport, smdp_address: str, matching_id: str,
                 eid_override: str = "", verify_ssl: bool = True) -> bool:
    """
    Full SGP.22 profile download flow.
    Returns True on success.
    """
    lpa    = LPAManager(transport)
    es10b  = ES10b(lpa)
    client = SmDpClient(smdp_address, matching_id, verify_ssl=verify_ssl)

    # ── 0. TERMINAL CAPABILITY (per lpac pcsc.c) ────────────────────────────
    # Tell the eUICC we support Local Profile Download (lpd_d).
    # 80 AA 00 00 0A  A9 08 81 00 82 01 01 83 01 07
    # Must be sent on basic channel BEFORE opening logical channel.
    logger.info("━" * 52)
    logger.info("Step 0    TERMINAL CAPABILITY (INS=AA)")
    tc_apdu = bytes.fromhex("80AA00000AA9088100820101830107")
    try:
        tc_resp = transport.send_raw(tc_apdu)
        logger.info("TERMINAL CAPABILITY response: SW=%s",
                     tc_resp.hex().upper() if tc_resp else "(empty)")
    except Exception as e:
        logger.warning("TERMINAL CAPABILITY failed (may be blocked by modem): %s", e)

    # ── 1. SELECT ISD-R ───────────────────────────────────────────────────────
    logger.info("━" * 52)
    logger.info("Step 1/7  SELECT ISD-R")
    lpa.select_isdr()

    # ── 2. GetEUICCChallenge ──────────────────────────────────────────────────
    logger.info("Step 2/7  GET EUICC CHALLENGE")
    challenge = es10b.get_euicc_challenge()
    logger.info("Challenge: %s", challenge.hex().upper())

    # ── 3. GetEUICCInfo1 ──────────────────────────────────────────────────────
    logger.info("Step 3/7  GET EUICC INFO1 (BF20)")
    info1 = es10b.get_euicc_info1()
    if info1:
        logger.info("eUICCInfo1: %d bytes", len(info1))
    else:
        logger.warning("eUICCInfo1 not available — proceeding without it")

    # ── 4. ES9+ initiateAuthentication ───────────────────────────────────────
    logger.info("Step 4/7  ES9+  initiateAuthentication → %s", smdp_address)
    server_data = client.initiate_authentication(challenge, info1)

    server_signed1 = base64.b64decode(server_data["serverSigned1"])
    server_sig1    = base64.b64decode(server_data["serverSignature1"])
    ci_pk_id       = base64.b64decode(server_data["euiccCiPKIdToBeUsed"])
    server_cert    = base64.b64decode(server_data["serverCertificate"])

    logger.info("serverSigned1:       %d bytes  head=%s",
                len(server_signed1), server_signed1[:4].hex().upper())
    logger.info("serverSignature1:    %d bytes  head=%s",
                len(server_sig1), server_sig1[:4].hex().upper())
    logger.info("euiccCiPKIdToBeUsed: %s", ci_pk_id.hex().upper())
    logger.info("serverCertificate:   %d bytes  head=%s",
                len(server_cert), server_cert[:4].hex().upper())

    # ── 5. ES10b AuthenticateServer ───────────────────────────────────────────
    logger.info("Step 5/7  AUTHENTICATE SERVER (ES10b.AuthenticateServer)")
    ctx_params1 = build_ctx_params1(matching_id)
    logger.debug("ctxParams1 (%d bytes): %s", len(ctx_params1), ctx_params1.hex().upper())
    euicc_auth = es10b.authenticate_server(
        server_signed1, server_sig1, ci_pk_id, server_cert,
        ctx_params1=ctx_params1)
    logger.info("eUICC auth response: %d bytes", len(euicc_auth))
    logger.debug("eUICC auth (hex): %s", euicc_auth.hex().upper())

    # ── 6. ES9+ authenticateClient ────────────────────────────────────────────
    logger.info("Step 6/7  ES9+  authenticateClient")
    auth_resp = client.authenticate_client(euicc_auth, eid=eid_override)

    meta = auth_resp.get("profileMetaData", {})
    logger.info("Profile metadata: %s", json.dumps(meta, indent=2))

    smdp_signed2 = base64.b64decode(auth_resp["smdpSigned2"])
    smdp_sig2    = base64.b64decode(auth_resp["smdpSignature2"])
    smdp_cert    = (base64.b64decode(auth_resp["smdpCertificate"])
                    if auth_resp.get("smdpCertificate") else b"")
    hash_cc      = base64.b64decode(auth_resp["hashCc"]) if auth_resp.get("hashCc") else b""

    logger.info("smdpSigned2:      %d bytes  head=%s",
                len(smdp_signed2), smdp_signed2[:4].hex().upper())
    logger.info("smdpSignature2:   %d bytes  head=%s",
                len(smdp_sig2), smdp_sig2[:4].hex().upper())
    if smdp_cert:
        logger.info("smdpCertificate:  %d bytes  head=%s",
                    len(smdp_cert), smdp_cert[:4].hex().upper())
    else:
        logger.info("smdpCertificate:  (not provided)")

    if hash_cc:
        logger.warning("Confirmation code required (hashCc present) — "
                       "sending empty hash (will fail if code is mandatory)")

    # ── 6b. ES10b PrepareDownload ─────────────────────────────────────────────
    logger.info("Step 6b   PREPARE DOWNLOAD (ES10b.PrepareDownload)")
    prepare_resp = es10b.prepare_download(smdp_signed2, smdp_sig2, hash_cc,
                                          smdp_cert=smdp_cert)
    logger.info("PrepareDownload response: %d bytes", len(prepare_resp))

    # ── 7. ES9+ getBoundProfilePackage ────────────────────────────────────────
    logger.info("Step 7a/7 ES9+  getBoundProfilePackage")
    bpp = client.get_bound_profile_package(prepare_resp)

    # ── 7b. ES10b LoadBoundProfilePackage ─────────────────────────────────────
    logger.info("Step 7b/7 LOAD BOUND PROFILE PACKAGE (%d bytes)", len(bpp))
    bpp_result = es10b.load_bound_profile_package(bpp)
    if bpp_result:
        logger.info("ProfileInstallationResult: %d bytes", len(bpp_result))

    # ── 7c. Extract installed ICCID from ProfileInstallationResult ───────────
    # Path: BF37 (PIR) → BF27 (PIR data) → BF2F (notificationMetadata) → 5A (iccid)
    # Scanning the BF2D list for the first/last 5A is unreliable: 5A can appear
    # as a data byte in unrelated TLV fields, and the list order of profiles is
    # not defined by SGP.22 — some eUICCs return insertion order (oldest first),
    # so neither the first nor the last 5A reliably identifies the new profile.
    iccid = b""
    try:
        iccid = _find_tag(bpp_result, 0xBF37, 0xBF27, 0xBF2F, 0x5A)
    except Exception:
        iccid = b""
    if not iccid:
        # Some eUICCs emit BF27 at top level without the BF37 wrapper
        try:
            iccid = _find_tag(bpp_result, 0xBF27, 0xBF2F, 0x5A)
        except Exception:
            iccid = b""
    if iccid:
        logger.info("Installed profile ICCID: %s (from ProfileInstallationResult)",
                    iccid.hex().upper())
    else:
        logger.warning("Could not parse ICCID from ProfileInstallationResult — "
                       "skipping auto-enable; use --enable <iccid> manually.")

    # ── 8. Enable Profile ─────────────────────────────────────────────────────
    logger.info("Step 8/8  ENABLE PROFILE")
    enabled = es10b.enable_profile(iccid=iccid)
    if enabled:
        logger.info("━" * 52)
        logger.info("Profile download + enable complete!")
        logger.info("Reboot the modem (AT+CFUN=1,1) to activate the new profile.")
        logger.info("━" * 52)
    else:
        logger.warning("━" * 52)
        logger.warning("Profile installed but enable failed — may need manual activation.")
        logger.warning("Check: profile_mgmt.py or use a PC/SC reader.")
        logger.warning("━" * 52)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _parse_lpa_string(lpa: str):
    """Parse LPA activation code: LPA:1$<address>$<matchingId>"""
    lpa = lpa.strip()
    if not lpa.upper().startswith("LPA:1$"):
        raise ValueError(f"Invalid LPA string (must start with LPA:1$): {lpa}")
    parts = lpa.split("$")
    if len(parts) < 3:
        raise ValueError(f"Invalid LPA string (need LPA:1$address$matchingId): {lpa}")
    return parts[1], parts[2]


def _iccid_to_bcd(iccid: str) -> bytes:
    """Convert human-readable ICCID to BCD-swapped bytes.
    E.g. '8955170230005315472' → bytes 985571200300355174F2"""
    # Pad to even length
    d = iccid
    if len(d) % 2:
        d += "F"
    # BCD swap each pair
    result = bytearray()
    for i in range(0, len(d), 2):
        result.append((int(d[i + 1], 16) << 4) | int(d[i], 16))
    return bytes(result)


def _build_transport(args, cfg):
    """Build transport from args + config."""
    if getattr(args, "mock", False):
        return MockTransport()
    t_cfg = cfg.get("transport", {})
    mode = t_cfg.get("mode", "real").lower()
    if mode == "real":
        return RealTransport(
            port=getattr(args, "port", "") or t_cfg.get("port", "/dev/ttyUSB2"),
            baudrate=int(t_cfg.get("baudrate", 115200)),
            timeout=float(t_cfg.get("timeout", 10.0)),
        )
    elif mode == "qmi":
        return QmiTransport(
            device=t_cfg.get("device", "/dev/cdc-wdm0"),
            slot=int(t_cfg.get("slot", 1)),
            timeout=float(t_cfg.get("timeout", 10.0)),
        )
    raise ValueError(f"Unknown transport mode: {mode}")


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="download.py",
        description="SGP.22 LPA — profile download & management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  # Download via LPA activation code (from QR code)
  sudo python3 download.py --lpa 'LPA:1$smdp.example.com$MATCHING-ID' --no-ssl-verify

  # Download via separate flags
  sudo python3 download.py --smdp smdp.example.com --mid MATCHING-ID --no-ssl-verify

  # List installed profiles
  sudo python3 download.py --list

  # Delete a profile by ICCID
  sudo python3 download.py --delete 8955170230005315472

  # Disable then delete
  sudo python3 download.py --disable 8955170230005315472
  sudo python3 download.py --delete 8955170230005315472
""",
    )

    # Download source (mutually exclusive group for clarity)
    ap.add_argument("--lpa", default="",
                    help="LPA activation code: 'LPA:1$<address>$<matchingId>'")
    ap.add_argument("--smdp", default="",
                    help="SM-DP+ FQDN (overrides config)")
    ap.add_argument("--mid", default="",
                    help="Matching ID (overrides config)")

    # Profile management
    ap.add_argument("--list", action="store_true",
                    help="List installed profiles and exit")
    ap.add_argument("--delete", metavar="ICCID",
                    help="Delete a profile by ICCID and exit")
    ap.add_argument("--disable", metavar="ICCID",
                    help="Disable a profile by ICCID and exit")
    ap.add_argument("--enable", metavar="ICCID",
                    help="Enable a profile by ICCID and exit")

    # General options
    ap.add_argument("--config", default="config.yaml",
                    help="Config file  [config.yaml]")
    ap.add_argument("--eid", default="",
                    help="EID override")
    ap.add_argument("--port", default="",
                    help="Serial port override")
    ap.add_argument("--no-ssl-verify", action="store_true",
                    help="Skip TLS cert verification (GSMA CI not in OS trust store)")
    ap.add_argument("--mock", action="store_true",
                    help="Use MockTransport (no hardware)")
    ap.add_argument("--debug", action="store_true",
                    help="Enable DEBUG logging")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    )

    # Load config
    cfg_path = Path(args.config)
    if cfg_path.exists():
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
    else:
        cfg = {}

    # ── Profile management commands ──────────────────────────────────────
    if args.list or args.delete or args.disable or args.enable:
        transport = _build_transport(args, cfg)
        try:
            with transport:
                lpa = LPAManager(transport)
                lpa.select_isdr()
                es10b = ES10b(lpa)

                if args.list:
                    profiles = es10b.list_profiles()
                    if not profiles:
                        print("No profiles installed.")
                        return 0
                    print(f"{'ICCID':<22} {'State':<10} {'Name':<20} {'Provider':<12} {'Class'}")
                    print("-" * 76)
                    for p in profiles:
                        print(f"{p.get('iccid','?'):<22} "
                              f"{p.get('state','?'):<10} "
                              f"{p.get('name',''):<20} "
                              f"{p.get('provider',''):<12} "
                              f"{p.get('class','')}")
                    return 0

                if args.enable:
                    iccid_bcd = _iccid_to_bcd(args.enable)
                    ok = es10b.enable_profile(iccid=iccid_bcd)
                    print(f"EnableProfile: {'OK' if ok else 'FAILED'}")
                    if ok:
                        print("Reboot modem (AT+CFUN=1,1) to activate.")
                    return 0 if ok else 1

                if args.disable:
                    iccid_bcd = _iccid_to_bcd(args.disable)
                    ok = es10b.disable_profile(iccid=iccid_bcd)
                    print(f"DisableProfile: {'OK' if ok else 'FAILED'}")
                    return 0 if ok else 1

                if args.delete:
                    iccid_bcd = _iccid_to_bcd(args.delete)
                    ok = es10b.delete_profile(iccid=iccid_bcd)
                    print(f"DeleteProfile: {'OK' if ok else 'FAILED'}")
                    return 0 if ok else 1

        except Exception as exc:
            logger.error("%s", exc)
            return 1

    # ── Download flow ────────────────────────────────────────────────────

    # Parse LPA string or use separate flags
    if args.lpa:
        try:
            smdp_address, matching_id = _parse_lpa_string(args.lpa)
        except ValueError as exc:
            logger.error("%s", exc)
            return 1
    else:
        smdp_address = args.smdp or cfg.get("dp_plus", {}).get("address", "")
        matching_id = args.mid or cfg.get("profile", {}).get("matching_id", "")

    eid_override = args.eid or cfg.get("euicc", {}).get("eid_override", "")
    verify_ssl = not args.no_ssl_verify

    if not smdp_address:
        logger.error("SM-DP+ address required — use --lpa, --smdp, or set in config.yaml")
        return 1
    if not matching_id:
        logger.error("Matching ID required — use --lpa, --mid, or set in config.yaml")
        return 1

    logger.info("SM-DP+ address : %s", smdp_address)
    logger.info("Matching ID    : %s", matching_id)
    logger.info("EID override   : %s", eid_override or "(none — reading from card)")
    logger.info("SSL verify     : %s", verify_ssl)

    transport = _build_transport(args, cfg)

    try:
        with transport:
            success = run_download(
                transport=transport,
                smdp_address=smdp_address,
                matching_id=matching_id,
                eid_override=eid_override,
                verify_ssl=verify_ssl,
            )
        return 0 if success else 1

    except KeyboardInterrupt:
        logger.warning("Interrupted")
        return 1
    except Exception as exc:
        logger.error("%s", exc)
        if args.debug:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
