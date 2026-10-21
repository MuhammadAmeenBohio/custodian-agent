"""IBAN helpers (1.2.1): mod-97 checksum, fake IBAN builder, and OCR repair.

Fake IBANs use country code XX, which is never a real country, so they can never be a real account.
They still carry a valid mod-97 check, like real IBANs. That is what lets extraction notice a misread
digit: a repaired IBAN is accepted only if its checksum passes, otherwise the caller gets None and the
invoice goes to a human.

repair_fake_iban never looks at vendor data. Repairing toward a known account would let a tampered
IBAN be "fixed" into a legitimate one.
"""
import re

COUNTRY = "XX"
FAKE_LEN = 18  # XX + 2 check digits + FAKE + 10 digits
_SHAPE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{1,30}$")
# What OCR typically confuses with a digit. Applied to digit positions only.
_AS_DIGIT = str.maketrans("OQDILBSZ", "00011852")


def _digits(s: str) -> str:
    """Letters become numbers (A=10 ... Z=35); digits stay."""
    return "".join(str(int(c, 36)) for c in s)


def check_digits(country: str, bban: str) -> str:
    return f"{98 - int(_digits(bban + country + '00')) % 97:02d}"


def is_valid(iban: str) -> bool:
    """Strict: the exact string must have a valid shape and a mod-97 remainder of 1."""
    return bool(_SHAPE.match(iban)) and int(_digits(iban[4:] + iban[:4])) % 97 == 1


def make_fake_iban(n: int) -> str:
    bban = f"FAKE{n:010d}"
    return f"{COUNTRY}{check_digits(COUNTRY, bban)}{bban}"


def clean(raw: str) -> str:
    return re.sub(r"[\s-]", "", raw).upper()


def repair_fake_iban(raw: str) -> str | None:
    """Fix 0/O-style misreads in the digit positions of a fake IBAN; None unless the checksum then passes."""
    s = clean(raw)
    if len(s) != FAKE_LEN or s[:2] != COUNTRY or s[4:8] != "FAKE":
        return None
    fixed = s[:2] + s[2:4].translate(_AS_DIGIT) + s[4:8] + s[8:].translate(_AS_DIGIT)
    return fixed if is_valid(fixed) else None
