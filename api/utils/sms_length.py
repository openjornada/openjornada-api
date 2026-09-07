"""
SMS encoding helpers: GSM-7 (default alphabet) detection and segment limits.

A single SMS segment carries 160 septets when the whole message can be
encoded with the GSM 03.38 default alphabet, but only 70 characters when any
character forces UCS-2 (16-bit) encoding. Characters from the GSM-7
*extension* table (``^ { } \\ [ ] ~ | €`` and form feed) are still GSM-7 but
cost two septets each.

Accent nuance (see design): ``à è é ì ò ù ç ñ ü`` are GSM-7 basic characters,
while the acute tildes ``á í ó ú`` are NOT — a single one of them drops the
budget from 160 to 70. :func:`fold_to_gsm7` transliterates exactly those
non-GSM-7 characters away so typical Latin-script text keeps the 160 budget.
"""

import unicodedata

# GSM 03.38 default alphabet — basic character set (one septet each).
# Includes the 13 ASCII characters that GSM-7 replaces (e.g. "ç" over "|"),
# which is why "|" is only in the extension table below.
GSM7_BASIC_CHARS = frozenset(
    "@£$¥èéùìòç\nØø\rÅÄÖÆßÉ"
    "åΔΦΓΛΩΠΨΣΘΞ"
    ' !"#$%&\'()*+,-./0123456789:;<=>?'
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "à¿abcdefghijklmnopqrstuvwxyzäöñüß"
    "_¤"
)

# GSM 03.38 extension table — still GSM-7, but each costs two septets.
GSM7_EXTENSION_CHARS = frozenset("\f^{}\\[]~|€")

GSM7_SEGMENT_SIZE = 160
UCS2_SEGMENT_SIZE = 70


def is_gsm7(text: str) -> bool:
    """True when every character of *text* fits the GSM-7 alphabet.

    Any character outside the basic + extension tables forces the whole
    message into UCS-2 encoding (limit drops to 70 chars).
    """
    return all(ch in GSM7_BASIC_CHARS or ch in GSM7_EXTENSION_CHARS for ch in text)


def sms_length(text: str) -> int:
    """Effective encoded length of *text*.

    GSM-7: septet count (extension characters count as 2). UCS-2: number of
    UTF-16 units (astral/emoji characters count as 2). Compare the result
    against :func:`sms_segment_limit`.
    """
    if not is_gsm7(text):
        return len(text.encode("utf-16-le")) // 2
    return sum(2 if ch in GSM7_EXTENSION_CHARS else 1 for ch in text)


def sms_segment_limit(text: str) -> int:
    """Max characters of *text* that still fit in a single SMS segment.

    Returns 160 for pure GSM-7 text, 70 when any UCS-2 character is present.
    """
    return GSM7_SEGMENT_SIZE if is_gsm7(text) else UCS2_SEGMENT_SIZE


def fits_single_segment(text: str) -> bool:
    """True when *text* is deliverable as exactly one SMS segment."""
    return sms_length(text) <= sms_segment_limit(text)


def truncate_to_single_segment(text: str) -> str:
    """Safely truncate *text* so it fits one SMS segment, appending an ellipsis.

    The ellipsis is chosen to match the encoding *text* already has: a
    GSM-7-compatible ``"..."`` (3 septets) when ``is_gsm7(text)``, so a
    GSM-7 message keeps its 160-septet budget, or the single-character
    ``"…"`` (which forces UCS-2, 70-character budget) otherwise. The result
    is guaranteed to satisfy :func:`fits_single_segment`.
    """
    if fits_single_segment(text):
        return text

    ellipsis = "..." if is_gsm7(text) else "…"
    budget = sms_segment_limit(text) - sms_length(ellipsis)
    body = text
    while body and sms_length(body) > budget:
        body = body[:-1]
    return body + ellipsis


# Non-GSM-7 characters that have a compatible GSM-7 form NFD decomposition
# cannot reach: typographic punctuation (common when templates are pasted
# from a word processor) and a few Latin letters without decompositions.
GSM7_FOLD_OVERRIDES: dict[str, str] = {
    # Curly quotes → ASCII straight quotes.
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    # Dash family → ASCII hyphen.
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2015": "-", "\u2212": "-",
    # Ellipsis → three dots ("…" itself is not GSM-7).
    "\u2026": "...",
    # Letters NFD does not decompose.
    "\u0131": "i",   # ı dotless i
    "\u0142": "l", "\u0141": "L",   # ł Ł
    "\u0111": "d", "\u0110": "D",   # đ Đ
}


def _fold_char(ch: str) -> str:
    """Best GSM-7-compatible form of a single non-GSM-7 character."""
    override = GSM7_FOLD_OVERRIDES.get(ch)
    if override is not None:
        return override
    # Strip combining marks: á → a, É → E, ş → s …
    base = "".join(
        c for c in unicodedata.normalize("NFD", ch)
        if not unicodedata.combining(c)
    )
    if len(base) == 1 and (
        base in GSM7_BASIC_CHARS or base in GSM7_EXTENSION_CHARS
    ):
        return base
    # No compatible form (emoji, CJK, …): keep it; it still forces UCS-2 and
    # the caller's truncation fallback applies.
    return ch


def fold_to_gsm7(text: str) -> str:
    """Transliterate non-GSM-7 characters to GSM-7-compatible equivalents.

    A single character outside the GSM-7 alphabet (e.g. the ``á`` in the
    common Spanish name "García") forces the *whole* message into UCS-2,
    dropping the single-segment budget from 160 to 70 septets and causing
    needless truncation. Folding such characters keeps typical Latin-script
    text in GSM-7:

    - characters already in GSM-7 are returned untouched — notably ``ñ``,
      ``à``, ``è``, ``é``, ``ü``, ``ç``, ``¿``, ``¡`` and ``€`` are NOT folded;
    - diacritics are stripped via NFD decomposition when the bare base
      character is GSM-7 (``á`` → ``a``, ``İ`` → ``I`` …);
    - :data:`GSM7_FOLD_OVERRIDES` covers what NFD cannot reach (curly quotes,
      dashes, ellipsis, ``ł`` …);
    - anything else (emoji, CJK, …) is kept as-is, so the message still goes
      UCS-2 and :func:`truncate_to_single_segment` remains the fallback.
    """
    if is_gsm7(text):
        return text
    return "".join(
        ch if ch in GSM7_BASIC_CHARS or ch in GSM7_EXTENSION_CHARS
        else _fold_char(ch)
        for ch in text
    )
