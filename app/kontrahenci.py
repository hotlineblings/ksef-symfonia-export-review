"""Generator importu kontrahentów do kartoteki Symfonia FK.

Format: sekcyjny AMS (jak pliki faktur — mechanizm SPRAWDZONY w tej instalacji
Symfonii) + WŁASNY szablon `SKsefKontrahenciImport.AMS`:
- `#format "AMS"` -> szablon pojawia się na liście wyboru jak pozostałe,
- `#DANEKH "1"` -> poziom uzgadniania danych kontrahenta jak w moście
  Handel->FK (hmffk.AMS), który tworzy karty kontrahentów,
- top-level `Section("Kontrahent", "Kontrahent")` (wzór: hmffk.AMS) czyta
  kolejne sekcje Kontrahent{} z pliku danych.

INWARIANTY klucza (za spec pomoc.symfonia.pl art. 11288): niepusty, != "0",
unikalny w pliku, stabilny między przebiegami -> klucz = NIP (same cyfry);
brak NIP -> "K_" + hash. Plik: Windows-1250 + CRLF.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from lxml import etree

from app.adres import parse_adres

WERSJA_SZABLONU = "1.0K"

# Szablon do JEDNORAZOWEGO skopiowania do folderu szablonów Symfonii
# (tam, gdzie SKsefInvoiceImport.AMS); nagłówek #opis = nazwa na liście.
TEMPLATE_AMS = f"""#opis  "Import kontrahentow z KSeF", "*.TXT (Pliki tekstowe)"  //
#format  "AMS"  //
#opisy  ""  //
#Disable  "2"  //poziom ostrzezen (bez ostrzezenia "Pole nie znalezione")
#POWIELONE_NAZWY  "0"  //0-zapisuje bez pytania (ponowny import = aktualizacja)
#DANEKH  "1"

Section("INFO", "")
{{
\tif (!equal([Wersja szablonu], "{WERSJA_SZABLONU}"))
\t{{
\t\tif(equal(ASK("Wskazany plik danych wymaga innej wersji szablonu importowego. Czy przerwac import danych? DefBtn:&Tak=10 Btn:&Nie=C ico:?"), "10"))
\t\t\tERROR("Operacja importu zostala przerwana.")
\t}}
}}

Section("Kontrahent", "Kontrahent")
{{
\t$klucz       = [klucz]
\t$skrot       = [skrot]
\t$nazwa       = [nazwa]
\t$nip         = [NIP]
\t$kraj        = [KodKraju]
\t$miejscowosc = [Miejscowosc]
\t$kod         = [KodPocztowy]
\t$ulica       = [Ulica]
\t$NumerDomu   = [NrDomu]
\t$NumerMieszk = [NrLokalu]
\t$email       = [Email]
\t$telefon1    = [Telefon]
\t$AdresL1     = [AdresL1]
\t$AdresL2     = [AdresL2]
\t$aktywny     = [aktywny]
}}
"""

TEMPLATE_FILENAME = "SKsefKontrahenciImport.AMS"


@dataclass(frozen=True)
class Kontrahent:
    nip: str            # znormalizowany (same cyfry) lub "" gdy brak
    nazwa: str
    kod_kraju: str
    adres_l1: str
    adres_l2: str
    email: str
    telefon: str
    ulica: str = ""
    nr_domu: str = ""
    nr_lokalu: str = ""
    kod_pocztowy: str = ""
    miejscowosc: str = ""

    @property
    def klucz(self) -> str:
        """Niepusty, != "0", stabilny między przebiegami. NIP albo hash z nazwy."""
        if self.nip and self.nip != "0":
            return self.nip
        digest = hashlib.sha1(f"{self.nazwa}|{self.miejscowosc}".encode("utf-8"))
        return "K_" + digest.hexdigest()[:12].upper()

    def zapelnienie(self) -> int:
        """Miara kompletności — przy duplikacie NIP wygrywa pełniejszy rekord."""
        return sum(
            1 for v in (self.ulica, self.nr_domu, self.kod_pocztowy,
                        self.miejscowosc, self.email, self.telefon, self.nazwa)
            if v
        )


def _digits(value: str | None) -> str:
    return "".join(c for c in (value or "") if c.isdigit())


def _strona_to_kontrahent(el: etree._Element) -> Kontrahent | None:
    nazwa = (el.findtext("DaneIdentyfikacyjne/PelnaNazwa") or "").strip()
    nip = _digits(el.findtext("DaneIdentyfikacyjne/NIP"))
    if not nazwa and not nip:
        return None
    adres_l1 = (el.findtext("Adres/AdresL1") or "").strip()
    adres_l2 = (el.findtext("Adres/AdresL2") or "").strip()

    def _get(tag: str) -> str:
        return (el.findtext(f"Adres/{tag}") or "").strip()

    if el.find("Adres/Ulica") is not None or el.find("Adres/Miejscowosc") is not None:
        ulica, nr_domu, nr_lokalu = _get("Ulica"), _get("NrDomu"), _get("NrLokalu")
        kod_pocztowy, miejscowosc = _get("KodPocztowy"), _get("Miejscowosc")
    else:
        s = parse_adres(adres_l1, adres_l2)
        ulica, nr_domu, nr_lokalu = s.ulica, s.nr_domu, s.nr_lokalu
        kod_pocztowy, miejscowosc = s.kod_pocztowy, s.miejscowosc

    return Kontrahent(
        nip=nip,
        nazwa=nazwa,
        kod_kraju=(el.findtext("Adres/KodKraju") or "PL").strip() or "PL",
        adres_l1=adres_l1,
        adres_l2=adres_l2,
        email=(el.findtext("DaneKontaktowe/TDaneKontaktoweV2/Email") or "").strip(),
        telefon=(el.findtext("DaneKontaktowe/TDaneKontaktoweV2/Telefon") or "").strip(),
        ulica=ulica,
        nr_domu=nr_domu,
        nr_lokalu=nr_lokalu,
        kod_pocztowy=kod_pocztowy,
        miejscowosc=miejscowosc,
    )


def kontrahenci_from_invoice(
    ksef_invoice: etree._Element, own_nip: str
) -> list[Kontrahent]:
    """OBIE strony faktury (sprzedawca i nabywca) z pominięciem NIP-u własnego."""
    own = _digits(own_nip)
    result: list[Kontrahent] = []
    for strona in ("Sprzedawca", "Nabywca"):
        el = ksef_invoice.find(f"invoiceV2/{strona}")
        if el is None:
            continue
        k = _strona_to_kontrahent(el)
        if k is None or (k.nip and k.nip == own):
            continue
        result.append(k)
    return result


def merge_kontrahent(existing: Kontrahent | None, new: Kontrahent) -> Kontrahent:
    """Dedup po kluczu: przy kolizji preferuj rekord z pełniejszym adresem."""
    if existing is None or new.zapelnienie() > existing.zapelnienie():
        return new
    return existing


def _clean(value: str) -> str:
    """Wartości AMS są do końca linii; klamry psułyby strukturę sekcji."""
    return value.replace("{", "(").replace("}", ")").replace("\r", " ").replace("\n", " ").strip()


def _skrot(k: Kontrahent) -> str:
    base = k.nazwa.strip() or k.nip
    return base[:20].strip()


def build_kontrahenci_ams(kontrahenci: list[Kontrahent]) -> str:
    """Plik danych: INFO{} + sekcje Kontrahent{} (LF; CP1250+CRLF nakłada zapis)."""
    unikalni: dict[str, Kontrahent] = {}
    for k in kontrahenci:
        unikalni[k.klucz] = merge_kontrahent(unikalni.get(k.klucz), k)

    lines: list[str] = [
        "INFO{",
        f"Wersja szablonu ={WERSJA_SZABLONU}",
        "}",
    ]
    for klucz, k in sorted(unikalni.items()):
        assert klucz and klucz != "0"
        lines += [
            "Kontrahent{",
            f"klucz ={_clean(klucz)}",
            f"skrot ={_clean(_skrot(k))}",
            f"nazwa ={_clean(k.nazwa)}",
            f"NIP ={_clean(k.nip)}",
            f"KodKraju ={_clean(k.kod_kraju)}",
            f"Miejscowosc ={_clean(k.miejscowosc)}",
            f"KodPocztowy ={k.kod_pocztowy}",
            f"Ulica ={_clean(k.ulica or k.adres_l1)}",
            f"NrDomu ={_clean(k.nr_domu)}",
            f"NrLokalu ={_clean(k.nr_lokalu)}",
            f"Email ={_clean(k.email)}",
            f"Telefon ={_clean(k.telefon)}",
            f"AdresL1 ={_clean(k.adres_l1)}",
            f"AdresL2 ={_clean(k.adres_l2)}",
            "aktywny =1",
            "}",
        ]
    return "\n".join(lines) + "\n"
