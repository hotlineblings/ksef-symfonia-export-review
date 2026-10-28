"""Parser adresów z FA(3) na pola strukturalne karty kontrahenta Symfonii.

FA(3) niesie adres jako dwie swobodne linie (AdresL1/AdresL2) — najczęściej
"ul. Nazwa 12 lok. 3" + "00-001 Miasto", ale bywa też wszystko w jednej linii.
Karta kontrahenta w FK ma pola: Ulica, NrDomu, NrLokalu, KodPocztowy,
Miejscowość — rozbijamy best-effort; gdy heurystyka nie pasuje, całość
zostaje w polu ulicy (nic nie ginie, bo AdresL1/L2 i tak są przekazywane).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_KOD_POCZTOWY = re.compile(r"\b(\d{2}-\d{3})\b")
_PREFIKS_ULICY = re.compile(r"^\s*(ul\.|ulica)\s+", re.IGNORECASE)
# "Nazwa 12 lok. 3" / "Nazwa 12 m. 3" / "Nazwa 12/3"
_DOM_LOKAL = re.compile(
    r"^(?P<ulica>.*?)[\s,]+(?P<dom>\d+[a-zA-Z]?)\s*"
    r"(?:[/\\]|lok\.?|m\.?|lokal\s*)\s*(?P<lokal>\d+[a-zA-Z]?)\.?\s*$"
)
_DOM = re.compile(r"^(?P<ulica>.*?)[\s,]+(?P<dom>\d+[a-zA-Z]?)\.?\s*$")


@dataclass(frozen=True)
class StrukturaAdresu:
    ulica: str = ""
    nr_domu: str = ""
    nr_lokalu: str = ""
    kod_pocztowy: str = ""
    miejscowosc: str = ""


def parse_adres(adres_l1: str, adres_l2: str) -> StrukturaAdresu:
    l1 = (adres_l1 or "").strip()
    l2 = (adres_l2 or "").strip()

    kod = ""
    miejscowosc = ""
    street_part = l1

    m2 = _KOD_POCZTOWY.search(l2)
    m1 = _KOD_POCZTOWY.search(l1)
    if m2:
        kod = m2.group(1)
        miejscowosc = _strip_seps(l2[m2.end():]) or _strip_seps(l2[: m2.start()])
        street_part = l1
    elif m1:
        kod = m1.group(1)
        miejscowosc = _strip_seps(l1[m1.end():]) or _strip_seps(l1[: m1.start()]) or l2
        # ulica to część PRZED kodem tylko, jeśli miejscowość wzięła się spoza niej
        before = _strip_seps(l1[: m1.start()])
        street_part = before if _strip_seps(l1[m1.end():]) else ""
        if not street_part:
            street_part = l2 if l2 else before
    else:
        # brak polskiego kodu pocztowego (adres zagraniczny) — bez rozbijania
        return StrukturaAdresu(ulica=l1, miejscowosc=l2)

    street_part = _PREFIKS_ULICY.sub("", street_part).strip()
    m = _DOM_LOKAL.match(street_part)
    if m:
        return StrukturaAdresu(
            ulica=m.group("ulica").strip(" ,"),
            nr_domu=m.group("dom"),
            nr_lokalu=m.group("lokal"),
            kod_pocztowy=kod,
            miejscowosc=miejscowosc,
        )
    m = _DOM.match(street_part)
    if m:
        return StrukturaAdresu(
            ulica=m.group("ulica").strip(" ,"),
            nr_domu=m.group("dom"),
            kod_pocztowy=kod,
            miejscowosc=miejscowosc,
        )
    return StrukturaAdresu(
        ulica=street_part.strip(" ,"), kod_pocztowy=kod, miejscowosc=miejscowosc
    )


def _strip_seps(value: str) -> str:
    return value.strip(" ,;-\t")
