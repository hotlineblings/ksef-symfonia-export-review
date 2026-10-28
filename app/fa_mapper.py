"""Mapowanie surowego XML faktury FA(3) z KSeF na strukturę ksefInvoice/invoiceV2,
którą konsumuje XSLT Symfonii (SKsefInvoiceImport.xslt, gałąź invoiceV2).

Fakty ze schematu FA(3) v1-0E (reference/fa3-xsd, zweryfikowane 2026-07-08):
- root Faktura{Naglowek, Podmiot1(sprzedawca), Podmiot2(nabywca), ..., Fa}
- Fa: KodWaluty*, P_1*(data wyst.), P_2*(numer), choice[P_6 | OkresFa{P_6_Od,P_6_Do}],
  grupy VAT P_13_x/P_14_x(/P_14_xW), P_15*(brutto), KursWalutyZ?, Adnotacje*(P_18A=MPP "1"/"2"),
  RodzajFaktury*(VAT|KOR|ZAL|ROZ|UPR|KOR_ZAL|KOR_ROZ), DaneFaKorygowanej(0..n)
- Podmiot1/DaneIdentyfikacyjne: NIP* + Nazwa*; Podmiot2: choice[NIP | KodUE+NrVatUE |
  KodKraju+NrID | BrakID] + Nazwa?; Adres{KodKraju*, AdresL1*, AdresL2?} (u nabywcy opcjonalny)
- kwoty TKwotowy = decimal(2 mc), kursy TIlosci = decimal(6 mc)

Kolejność elementów wynikowych MUSI odpowiadać próbkom golden (samples/) — XSLT
emituje pola nagłówka przez apply-templates w kolejności dokumentu.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Literal

from lxml import etree

from app.adres import parse_adres

logger = logging.getLogger(__name__)

TWO_PLACES = Decimal("0.01")
SIX_PLACES = Decimal("0.000001")


class Fa3MappingError(Exception):
    """Faktura niemożliwa do automatycznego zmapowania — do ręcznego zaksięgowania."""


@dataclass(frozen=True)
class InvoiceContext:
    ksef_number: str
    ksef_date: str  # YYYY-MM-DD (data przyjęcia w KSeF -> "data wpływu" w FK)
    subject_type: Literal["Subject1", "Subject2"]

    @property
    def invoice_bound(self) -> str:
        return "INTERNAL" if self.subject_type == "Subject1" else "EXTERNAL"


@dataclass
class MapResult:
    element: etree._Element
    warnings: list[str] = field(default_factory=list)


@dataclass
class _VatBucket:
    stawka: str
    netto: Decimal
    vat: Decimal
    vat_kurs: Decimal  # kwota VAT w PLN (dla PLN == vat)


# grupy stawek: (pole netto, pole VAT, pole VAT w PLN, stawka domyślna, stawka alternatywna)
_RATE_GROUPS = [
    ("P_13_1", "P_14_1", "P_14_1W", "23", "22"),
    ("P_13_2", "P_14_2", "P_14_2W", "8", "7"),
    ("P_13_3", "P_14_3", "P_14_3W", "5", None),
    ("P_13_4", "P_14_4", "P_14_4W", "4", "3"),
]
# stawki bez kwoty VAT: (pole netto, literał stawki dla Symfonii)
_ZERO_GROUPS = [
    ("P_13_6_1", "0"),  # 0% krajowa
    ("P_13_6_2", "0"),  # 0% WDT
    ("P_13_6_3", "0"),  # 0% eksport
    ("P_13_7", "zw"),   # zwolnione  (AMS: zw -> stawka -1)
    ("P_13_8", "np"),   # poza terytorium kraju  (AMS: np -> stawka -2)
    ("P_13_9", "np"),   # usługi art. 100 ust. 1 pkt 4
]
# procedury, których nie księgujemy automatycznie
_UNSUPPORTED = [
    ("P_13_5", "procedura szczególna (OSS / podatek od wartości dodanej)"),
    ("P_13_10", "odwrotne obciążenie"),
    ("P_13_11", "procedura marży"),
]

_KOR_TYPES = {"KOR", "KOR_ZAL", "KOR_ROZ"}


def map_fa3_to_ksef_invoice(
    fa_xml: bytes, ctx: InvoiceContext, *, structured_address: bool = False
) -> MapResult:
    """FA(3) -> drzewo ksefInvoice gotowe dla AmsTransformer. Rzuca Fa3MappingError.

    structured_address=True dodaje do Adresu kontrahenta pola strukturalne
    (Ulica/NrDomu/NrLokalu/KodPocztowy/Miejscowosc). DOMYŚLNIE WYŁĄCZONE:
    Miejscowość wchodzi do klucza kontrahenta w szablonie Symfonii
    ($klucz = NIP+Miejscowość) i zaobserwowano ciche odrzucanie dokumentów
    po jej dodaniu — plik faktury domyślnie jest 1:1 ze sprawdzonym formatem.
    """
    warnings: list[str] = []
    root = _localname_tree(fa_xml)
    if root.tag != "Faktura":
        raise Fa3MappingError(f"Nieoczekiwany element główny: {root.tag!r}")
    _check_form_code(root, warnings)

    fa = _req(root, "Fa", "brak sekcji Fa")
    podmiot1 = _req(root, "Podmiot1", "brak Podmiot1 (sprzedawca)")
    podmiot2 = _req(root, "Podmiot2", "brak Podmiot2 (nabywca)")

    numer = _text(fa, "P_2") or _fail("brak numeru faktury (P_2)")
    data_wyst = _text(fa, "P_1") or _fail("brak daty wystawienia (P_1)")
    kod_waluty = _text(fa, "KodWaluty") or _fail("brak kodu waluty (KodWaluty)")
    rodzaj = _text(fa, "RodzajFaktury") or _fail("brak RodzajFaktury")

    for field_name, desc in _UNSUPPORTED:
        if _dec_or_none(_text(fa, field_name)) not in (None, Decimal("0")):
            raise Fa3MappingError(
                f"Faktura z polem {field_name} ({desc}) — do ręcznego zaksięgowania"
            )

    is_pln = kod_waluty.upper() == "PLN"
    kurs = _resolve_kurs(fa, is_pln, warnings)
    buckets = _aggregate_vat(fa, is_pln, kurs, warnings)
    if not buckets and rodzaj != "ROZ":
        raise Fa3MappingError(
            "Brak kwot podsumowania VAT (P_13_x) — do ręcznego zaksięgowania"
        )
    _check_p15(fa, buckets, warnings, numer)

    # ---- budowa drzewa (kolejność == golden samples!) ----
    inv = etree.Element("ksefInvoice")
    _sub(inv, "invoiceBound", ctx.invoice_bound)
    _sub(inv, "KsefNumber", ctx.ksef_number)
    _sub(inv, "KSeFDate", ctx.ksef_date)
    _sub(inv, "KSeFStatus", "approved")
    # ŚWIEŻY GUID per wygenerowany plik — eArchiveId identyfikuje egzemplarz
    # importu w buforze FK (kolumna uniqueidentifier); GUID deterministyczny
    # per faktura blokował ponowny import tego samego dokumentu
    _sub(inv, "eArchiveId", str(uuid.uuid4()).upper())

    v2 = etree.SubElement(inv, "invoiceV2")

    nag = etree.SubElement(v2, "Naglowek")
    _sub(nag, "Numer", numer)
    _sub(nag, "DataWystawienia", data_wyst)
    p6 = _text(fa, "P_6")
    if p6:
        _sub(nag, "DataDostawy", p6)
    _sub(nag, "KodWaluty", kod_waluty)
    _sub(nag, "RodzajFaktury", rodzaj)
    okres = fa.find("OkresFa")
    if okres is not None:
        okres_el = etree.SubElement(nag, "OkresFaktury")
        _sub(okres_el, "DataOd", _text(okres, "P_6_Od") or "")
        _sub(okres_el, "DataDo", _text(okres, "P_6_Do") or "")

    _build_strona(v2, "Sprzedawca", podmiot1, warnings, structured_address)
    _build_strona(v2, "Nabywca", podmiot2, warnings, structured_address)

    pv_outer = etree.SubElement(v2, "PodsumowanieVat")
    for b in buckets:
        pv = etree.SubElement(pv_outer, "PodsumowanieVat")
        _sub(pv, "StawkaPodatku", b.stawka)
        _sub(pv, "WartoscNetto", _fmt2(b.netto))
        _sub(pv, "KwotaVat", _fmt2(b.vat))
        _sub(pv, "KwotaVatKurs", _fmt2(b.vat_kurs))

    wiersze = etree.SubElement(v2, "Wiersze")
    if rodzaj == "ROZ" and not buckets:
        # ścieżka "zerowego rejestru" w XSLT: po jednym WierszV2 per stawka z pozycji
        stawki = _distinct_p12(fa) or [""]
        for i, stawka in enumerate(stawki):
            w = etree.SubElement(wiersze, "WierszV2")
            if i == 0:
                _sub(w, "KursWaluty", kurs)
            _sub(w, "StawkaPodatku", stawka)
    else:
        w = etree.SubElement(wiersze, "WierszV2")
        _sub(w, "KursWaluty", kurs)

    adn = etree.SubElement(v2, "Adnotacje")
    mpp = _text(fa.find("Adnotacje"), "P_18A") if fa.find("Adnotacje") is not None else None
    _sub(adn, "PodzielonaPlatnosc", "true" if mpp == "1" else "false")

    if rodzaj in _KOR_TYPES:
        _build_korekta(v2, fa, warnings)

    if not is_pln:
        warnings.append(
            f"Faktura walutowa ({kod_waluty}, kurs {kurs}) — zweryfikuj księgowanie "
            "kursów w Symfonii przed zatwierdzeniem"
        )
    return MapResult(inv, warnings)


# --------------------------------------------------------------------------- #

def _localname_tree(fa_xml: bytes) -> etree._Element:
    try:
        root = etree.fromstring(fa_xml)
    except etree.XMLSyntaxError as exc:
        raise Fa3MappingError(f"Niepoprawny XML faktury: {exc}") from exc
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    etree.cleanup_namespaces(root)
    return root


def _check_form_code(root: etree._Element, warnings: list[str]) -> None:
    kf = root.find("Naglowek/KodFormularza")
    kod = kf.get("kodSystemowy") if kf is not None else None
    if kod and kod != "FA (3)":
        warnings.append(f"Schemat faktury {kod!r} zamiast 'FA (3)' — mapowanie może być niepełne")


def _req(parent: etree._Element, name: str, msg: str) -> etree._Element:
    el = parent.find(name)
    if el is None:
        raise Fa3MappingError(msg)
    return el


def _fail(msg: str):
    raise Fa3MappingError(msg)


def _text(parent: etree._Element | None, name: str) -> str | None:
    if parent is None:
        return None
    el = parent.find(name)
    if el is None or el.text is None:
        return None
    value = el.text.strip()
    return value or None


def _dec_or_none(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _fmt2(value: Decimal) -> str:
    return str(value.quantize(TWO_PLACES))


def _sub(parent: etree._Element, name: str, text: str) -> etree._Element:
    el = etree.SubElement(parent, name)
    el.text = text
    return el


def _distinct_p12(fa: etree._Element) -> list[str]:
    seen: list[str] = []
    for w in fa.findall("FaWiersz"):
        v = _text(w, "P_12")
        if v and v not in seen:
            seen.append(v)
    return seen


def _resolve_kurs(fa: etree._Element, is_pln: bool, warnings: list[str]) -> str:
    if is_pln:
        return "1"
    for w in fa.findall("FaWiersz"):
        v = _text(w, "KursWaluty")
        if v:
            return v
    v = _text(fa, "KursWalutyZ")
    if v:
        return v
    # ostatnia deska: kurs wyliczony z pary P_14_xW / P_14_x
    for _, p14, p14w, _, _ in _RATE_GROUPS:
        vat = _dec_or_none(_text(fa, p14))
        vat_w = _dec_or_none(_text(fa, p14w))
        if vat and vat_w and vat != 0:
            kurs = (vat_w / vat).quantize(SIX_PLACES)
            warnings.append(f"Kurs waluty wyliczony z {p14w}/{p14}: {kurs}")
            return str(kurs)
    raise Fa3MappingError(
        "Faktura walutowa bez kursu (brak KursWaluty/KursWalutyZ/P_14_xW) "
        "— do ręcznego zaksięgowania"
    )


def _aggregate_vat(
    fa: etree._Element, is_pln: bool, kurs: str, warnings: list[str]
) -> list[_VatBucket]:
    p12s = set(_distinct_p12(fa))
    buckets: dict[str, _VatBucket] = {}

    def add(stawka: str, netto: Decimal, vat: Decimal, vat_kurs: Decimal) -> None:
        if stawka in buckets:
            b = buckets[stawka]
            b.netto += netto
            b.vat += vat
            b.vat_kurs += vat_kurs
        else:
            buckets[stawka] = _VatBucket(stawka, netto, vat, vat_kurs)

    for p13, p14, p14w, default_rate, alt_rate in _RATE_GROUPS:
        netto = _dec_or_none(_text(fa, p13))
        if netto is None:
            continue
        vat = _dec_or_none(_text(fa, p14)) or Decimal("0")
        stawka = default_rate
        if alt_rate and alt_rate in p12s and default_rate not in p12s:
            stawka = alt_rate
        if is_pln:
            vat_kurs = vat
        else:
            vat_w = _dec_or_none(_text(fa, p14w))
            if vat_w is not None:
                vat_kurs = vat_w
            else:
                vat_kurs = (vat * Decimal(kurs)).quantize(TWO_PLACES)
                if vat != 0:
                    warnings.append(
                        f"Brak {p14w} — KwotaVatKurs wyliczona jako {p14}×kurs"
                    )
        add(stawka, netto, vat, vat_kurs)

    for p13, stawka in _ZERO_GROUPS:
        netto = _dec_or_none(_text(fa, p13))
        if netto is None or netto == 0:
            continue
        add(stawka, netto, Decimal("0"), Decimal("0"))

    return list(buckets.values())


def _check_p15(
    fa: etree._Element, buckets: list[_VatBucket], warnings: list[str], numer: str
) -> None:
    p15 = _dec_or_none(_text(fa, "P_15"))
    if p15 is None or not buckets:
        return
    total = sum((b.netto + b.vat for b in buckets), Decimal("0"))
    tolerance = TWO_PLACES * max(1, len(buckets))
    if abs(total - p15) > tolerance:
        warnings.append(
            f"Suma netto+VAT ({total}) różni się od P_15 ({p15}) w fakturze {numer} "
            "— sprawdź dekret (zaliczka/rozliczeniowa?)"
        )


def _build_strona(
    v2: etree._Element,
    name: str,
    podmiot: etree._Element,
    warnings: list[str],
    structured_address: bool = False,
) -> None:
    dane = podmiot.find("DaneIdentyfikacyjne")
    nip = _text(dane, "NIP")
    nazwa = _text(dane, "Nazwa")
    if not nazwa:
        warnings.append(f"{name} bez nazwy (faktura uproszczona?) — uzupełnij kontrahenta")
    if not nip:
        # nabywca zagraniczny / bez identyfikatora: NrVatUE lub NrID zamiast NIP
        alt = _text(dane, "NrVatUE") or _text(dane, "NrID")
        if alt:
            warnings.append(f"{name} bez polskiego NIP (identyfikator {alt}) — kontrahent zagraniczny")

    strona = etree.SubElement(v2, name)
    ident = etree.SubElement(strona, "DaneIdentyfikacyjne")
    _sub(ident, "NIP", nip or "")
    _sub(ident, "PelnaNazwa", nazwa or "")
    _sub(ident, "BrakId", "false" if nip else "true")

    adres_src = podmiot.find("Adres")
    adres = etree.SubElement(strona, "Adres")
    _sub(adres, "KodKraju", _text(adres_src, "KodKraju") or "")
    adres_l1 = _text(adres_src, "AdresL1") or ""
    adres_l2 = _text(adres_src, "AdresL2") or ""
    _sub(adres, "AdresL1", adres_l1)
    _sub(adres, "AdresL2", adres_l2)
    if structured_address:
        # pola strukturalne karty kontrahenta FK (ulica/nr/kod/miejscowość) —
        # rozbite best-effort z linii FA(3); XSLT emituje je tylko gdy obecne
        struktura = parse_adres(adres_l1, adres_l2)
        for tag, value in (
            ("Ulica", struktura.ulica),
            ("NrDomu", struktura.nr_domu),
            ("NrLokalu", struktura.nr_lokalu),
            ("KodPocztowy", struktura.kod_pocztowy),
            ("Miejscowosc", struktura.miejscowosc),
        ):
            if value:
                _sub(adres, tag, value)

    email, telefon = None, None
    for dk in podmiot.findall("DaneKontaktowe"):
        email = email or _text(dk, "Email")
        telefon = telefon or _text(dk, "Telefon")
    kontakt = etree.SubElement(strona, "DaneKontaktowe")
    tdk = etree.SubElement(kontakt, "TDaneKontaktoweV2")
    _sub(tdk, "Email", email or "")
    _sub(tdk, "Telefon", telefon or "")


def _build_korekta(v2: etree._Element, fa: etree._Element, warnings: list[str]) -> None:
    dane_list = fa.findall("DaneFaKorygowanej")
    if not dane_list:
        warnings.append("Faktura korygująca bez DaneFaKorygowanej — brak odwołania do korygowanej")
        return
    korekta = etree.SubElement(v2, "Korekta")
    fa_kor = etree.SubElement(korekta, "FaKorygowana")
    for dane in dane_list:
        el = etree.SubElement(fa_kor, "DaneFaKorygowanejV2")
        _sub(el, "NrFaKorygowanej", _text(dane, "NrFaKorygowanej") or "")
        _sub(el, "DataWystFaKorygowanej", _text(dane, "DataWystFaKorygowanej") or "")
        nr_ksef = _text(dane, "NrKSeFFaKorygowanej")
        if not nr_ksef and _text(dane, "NrKSeFN") == "1":
            warnings.append("Korygowana faktura wystawiona poza KSeF (NrKSeFN)")
        _sub(el, "NrKSeFFaKorygowanej", nr_ksef or "")
