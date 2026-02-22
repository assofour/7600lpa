"""
lpa_manager.py — SGP.22 Local Profile Assistant core logic

Implements APDU sequences for:
  • SELECT ISD-R
  • GET EID  (tag BF3E → 5A)
  • GET eUICCInfo2  (tag BF22)

All APDU responses are parsed with a minimal BER-TLV decoder.
"""

from __future__ import annotations

import logging
from typing import Optional

from transport import Transport, APDUResponse

logger = logging.getLogger(__name__)

# ISD-R Application Identifier (GSMA SGP.32 Annex B)
ISDR_AID = bytes.fromhex("A0000005591010FFFFFFFF8900000100")


# ---------------------------------------------------------------------------
# BER-TLV helper
# ---------------------------------------------------------------------------

def _parse_tlv(data: bytes) -> dict[int, bytes]:
    """
    Flat BER-TLV parser.  Returns {tag_int: value_bytes} for the top-level tags.
    Does NOT recurse into constructed TLVs — call again on the value if needed.
    """
    result: dict[int, bytes] = {}
    i = 0
    while i < len(data):
        if i >= len(data):
            break

        # --- Tag ---
        b = data[i]
        i += 1
        tag = b
        if b & 0x1F == 0x1F:          # Multi-byte tag
            while i < len(data) and data[i] & 0x80:
                tag = (tag << 8) | data[i]
                i += 1
            if i < len(data):
                tag = (tag << 8) | data[i]
                i += 1

        # --- Length ---
        if i >= len(data):
            break
        l0 = data[i]; i += 1
        if l0 & 0x80:
            n = l0 & 0x7F
            length = int.from_bytes(data[i : i + n], "big")
            i += n
        else:
            length = l0

        # --- Value ---
        value = data[i : i + length]
        i += length
        result[tag] = value

    return result


def _find_tag(data: bytes, *tag_path: int) -> bytes:
    """
    Drill into nested TLV by following tag_path.
    E.g. _find_tag(data, 0xBF3E, 0x5A) → parse BF3E, then 5A inside it.
    Raises KeyError if any tag is missing.
    """
    current = data
    for tag in tag_path:
        tlv = _parse_tlv(current)
        if tag not in tlv:
            raise KeyError(f"Tag 0x{tag:X} not found in TLV")
        current = tlv[tag]
    return current


# ---------------------------------------------------------------------------
# IPAManager
# ---------------------------------------------------------------------------

class LPAManager:
    """
    Local Profile Assistant — orchestrates SGP.22 APDU sequences.

    Usage:
        with transport:
            lpa = LPAManager(transport)
            lpa.select_isdr()
            eid = lpa.get_eid()
    """

    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    # ------------------------------------------------------------------
    # ISD-R selection
    # ------------------------------------------------------------------

    def select_isdr(self) -> APDUResponse:
        """
        SELECT ISD-R by AID.
        APDU: 00 A4 04 00 <Lc=10> A0000005591010FFFFFFFF8900000100
        """
        apdu = bytes([0x00, 0xA4, 0x04, 0x00, len(ISDR_AID)]) + ISDR_AID
        resp = self.transport.send_apdu(apdu)
        if not resp.success:
            raise RuntimeError(f"SELECT ISD-R failed: SW={resp.sw_hex}")
        logger.info("ISD-R selected  (SW=%s)", resp.sw_hex)
        return resp

    # ------------------------------------------------------------------
    # EID retrieval
    # ------------------------------------------------------------------

    def get_eid(self) -> str:
        """
        Retrieve EID from ISD-R via GET DATA tag BF3E.
        APDU: 80 CA BF 3E 00
        Response TLV: BF3E { 5A <EID bytes> }
        Returns EID as uppercase hex string (32 chars = 16 bytes).
        """
        apdu = bytes([0x80, 0xCA, 0xBF, 0x3E, 0x00])
        resp = self.transport.send_apdu(apdu)
        if not resp.success:
            if resp.sw == 0x6A88:
                raise RuntimeError(
                    "EID not found on card (SW=6A88). "
                    "The ISD-R is present but the EID data object is not provisioned. "
                    "Check the card packaging or set euicc.eid_override in config.yaml."
                )
            raise RuntimeError(f"GET DATA (EID / BF3E) failed: SW={resp.sw_hex}")

        try:
            eid_bytes = _find_tag(resp.data, 0xBF3E, 0x5A)
        except KeyError as exc:
            raise RuntimeError(f"EID TLV parse error: {exc}") from exc

        eid_str = eid_bytes.hex().upper()
        logger.info("EID: %s", eid_str)
        return eid_str

    # ------------------------------------------------------------------
    # eUICCInfo2
    # ------------------------------------------------------------------

    def get_euicc_info2(self) -> dict:
        """
        Retrieve eUICCInfo2 (tag BF22) per SGP.32 §3.1.3.
        APDU: 80 CA BF 22 00

        Returns a dict with parsed sub-fields (raw bytes values).
        Raises RuntimeError if the command fails.
        """
        apdu = bytes([0x80, 0xCA, 0xBF, 0x22, 0x00])
        resp = self.transport.send_apdu(apdu)
        if not resp.success:
            raise RuntimeError(f"GET DATA (eUICCInfo2 / BF22) failed: SW={resp.sw_hex}")

        logger.debug("eUICCInfo2 raw (%d bytes): %s", len(resp.data), resp.data.hex().upper())

        # Outer tag BF22
        try:
            inner_data = _find_tag(resp.data, 0xBF22)
        except KeyError as exc:
            raise RuntimeError(f"BF22 tag not found in response: {exc}") from exc

        fields = _parse_tlv(inner_data)

        # Map known sub-tags to friendly names (SGP.22 §5.7.2 / SGP.32 §3.1.3)
        _TAG_NAMES = {
            0x82: "profile_version",               # SGP.22
            0x83: "svn",                           # SGP.22 spec version number
            0x84: "euicc_firmware_ver",
            0x85: "ext_card_resource",
            0x86: "uicc_capability",
            0x87: "ts102241_version",
            0x88: "global_platform_version",
            0x89: "rsp_capability",
            0xA0: "euicc_ci_pki_list_for_verification",
            0xA1: "euicc_ci_pki_list_for_signing",
            # SGP.32 variants
            0x99: "euicc_ci_pki_list_for_verification",
            0x9A: "euicc_ci_pki_list_for_signing",
        }

        parsed: dict = {}
        for tag, value in fields.items():
            name = _TAG_NAMES.get(tag, f"tag_{tag:02X}")
            parsed[name] = value
            logger.debug("  [BF22] %s (0x%02X): %s", name, tag, value.hex().upper())

        return parsed
