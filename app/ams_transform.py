"""ksefInvoice XML -> (XSLT Symfonii) -> tekst AMS -> bajty .TXT (Windows-1250 + CRLF).

Transform wykonujemy po naszej stronie (pole XSLT w profilu Symfonii jest
wyszarzone) i podajemy Symfonii gotowy plik AMS .TXT importowany szablonem
SKsefInvoiceImport.AMS (Wersja szablonu 3.1).
"""

from __future__ import annotations

import codecs
import logging
import os
import sys
import unicodedata
from pathlib import Path

from lxml import etree

logger = logging.getLogger(__name__)

_FALLBACK_ERRORS = "ams_cp1250_fallback"

# Znaki spoza CP1250 podmieniane ręcznie (typografia/waluty); reszta przez NFKD.
_MANUAL_MAP = {
    "€": "EUR",  # € jest w CP1250 (0x80), ale zostawiamy mapę dla jasności
    " ": " ",    # twarda spacja
    "‑": "-",    # non-breaking hyphen
    "−": "-",    # minus matematyczny
    "ʼ": "'",
    # litery bez dekompozycji NFKD i bez odpowiednika w CP1250
    "œ": "oe", "Œ": "OE", "æ": "ae", "Æ": "AE",
    "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ı": "i",
}


def _cp1250_fallback(err: UnicodeError) -> tuple[str, int]:
    assert isinstance(err, UnicodeEncodeError)
    out: list[str] = []
    for ch in err.object[err.start : err.end]:
        repl = _MANUAL_MAP.get(ch)
        if repl is None:
            decomposed = unicodedata.normalize("NFKD", ch)
            stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
            try:
                stripped.encode("cp1250")
                repl = stripped or "?"
            except UnicodeEncodeError:
                repl = "?"
        out.append(repl)
    return "".join(out), err.end


codecs.register_error(_FALLBACK_ERRORS, _cp1250_fallback)


def project_root() -> Path:
    """Katalog główny projektu; w aplikacji spakowanej PyInstallerem - katalog zasobów."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def default_xslt_path() -> Path:
    return project_root() / "reference" / "symfonia-szablony" / "SKsefInvoiceImport.xslt"


def encode_cp1250(text: str) -> tuple[bytes, list[str]]:
    """Koduje do CP1250. Zwraca (bajty, lista znaków, których nie dało się zakodować 1:1)."""
    try:
        return text.encode("cp1250"), []
    except UnicodeEncodeError:
        bad = sorted({ch for ch in text if not _encodable(ch)})
        return text.encode("cp1250", errors=_FALLBACK_ERRORS), bad


def _encodable(ch: str) -> bool:
    try:
        ch.encode("cp1250")
        return True
    except UnicodeEncodeError:
        return False


class AmsTransformer:
    """Kompiluje XSLT Symfonii raz i transformuje drzewa ksefInvoice na AMS."""

    def __init__(self, xslt_path: Path | None = None):
        self.xslt_path = Path(xslt_path) if xslt_path else default_xslt_path()
        with open(self.xslt_path, "rb") as fh:
            xslt_doc = etree.parse(fh)
        self._transform = etree.XSLT(xslt_doc)

    def to_ams_text(self, ksef_invoice: etree._Element | etree._ElementTree) -> str:
        result = self._transform(ksef_invoice)
        return str(result)

    def to_txt_bytes(
        self, ksef_invoice: etree._Element | etree._ElementTree, *, context: str = ""
    ) -> bytes:
        text = self.to_ams_text(ksef_invoice)
        # XSLT emituje LF; plik dla Symfonii musi mieć CRLF.
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
        data, bad_chars = encode_cp1250(text)
        if bad_chars:
            logger.warning(
                "Znaki spoza CP1250 zastąpione transliteracją%s: %s",
                f" ({context})" if context else "",
                " ".join(repr(c) for c in bad_chars),
            )
        return data


def write_txt_atomic(path: Path, data: bytes) -> None:
    """Zapis atomowy: plik tymczasowy w tym samym katalogu + os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
