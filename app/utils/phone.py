import re

from app.core.errors import Invalid

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")


def normalize_phone(raw: str, default_cc: str = "+91") -> str:
    """Normalise to E.164. 10-digit Indian mobiles (6-9 prefix) get +91."""
    s = re.sub(r"[\s\-().]", "", raw or "")
    if s.startswith("00"):
        s = "+" + s[2:]
    if not s.startswith("+"):
        digits = s.lstrip("0") if len(s) == 11 and s.startswith("0") else s
        if re.fullmatch(r"[6-9]\d{9}", digits):
            s = default_cc + digits
        elif re.fullmatch(r"91[6-9]\d{9}", digits):
            s = "+" + digits
        else:
            s = "+" + digits
    if not _E164.match(s):
        raise Invalid("Enter a valid mobile number (with country code if not Indian)", code="invalid_phone")
    return s
