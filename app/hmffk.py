"""FA(3) -> JEDEN plik AMS "format FK 3.0" pod szablon hmffk.AMS
("Dane z programu Handel, format FK 3.0" — standardowy szablon Symfonii FK).

Zaleta wobec SKsefInvoiceImport: hmffk.AMS jest w każdej instalacji FK
(niczego nie kopiujemy do folderu szablonów), a JEDEN plik niesie razem
sekcje Kontrahent{} (pełne karty kontrahentów) i dokument{} (faktury
z rejestrem VAT i numerami KSeF). Dokumenty niosą też szkielet dekretu
(sekcje zapis{} — strony WN/MA i kwoty); linia brutto dostaje konto
rozrachunkowe "Konto =<prefiks>-<pozycja>", gdy exporter poda
konto_rozrachunkowe (rejestr numerów kontrahentów) — w przeciwnym razie
(albo gdy suma brutto = 0) zostaje bez konta i księgowa dekretuje ją ręcznie.
Pełny dekret sprzedaży: dokumenty sprzedażowe dostają dodatkowo konto na
linii MA netto (konto_netto) i MA VAT (konto_vat) — exporter ustawia je tylko
dla sprzedaży; dla kosztu obie linie zostają bez konta (dekretuje księgowa).
Rozrachunki (reference/rozrachunki-hmffk-E2E.md): ten sam zapis brutto (linia
rozrachunkowa) z niepustym konto_rozrachunkowe dostaje też NumerDok/
IdDlaRozliczen, a gdy termin_platnosci jest podany — sekcję transakcja{}
zaraz po nim, żeby FK automatycznie założył rozrachunek z terminem.

Mapowanie pól 1:1 wg analizy hmffk.AMS (reference/symfonia-szablony/hmffk.AMS):
- INFO wymaga "Wersja szablonu" = 4; Wersja_programu >= 308 (bez flagi legacy),
- Kontrahent: klucz=[id] — u nas STABILNY (NIP cyfry albo K_<hash>), żeby
  uzgodnienia kontrahentów w FK nie rozjeżdżały się między eksportami,
- dokument sprzedaży: DataDok=[dataWystawienia], DataOper=[dataSprzedazy],
- dokument zakupu: DataDok=[dataWystawieniaObca], DataOper=[dataZakupu],
  Datawpl=[Datawpl] (= data przyjęcia w KSeF -> moment odliczenia VAT),
- rejestr: typ ZW gdy [okres] podany; oczek=0 (metoda_vat=0 / naliczenie_VAT=0),
- pola KSeF ([KsefNumber], [KSeFDate], [eArchiveId]) czytane dla rodzaju
  zakupu/sprzedaży — numer KSeF trafia na dokument w FK.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field, fields
from datetime import date, timedelta
from decimal import Decimal

from app.adres import parse_adres
from app.fa_mapper import (
    _KOR_TYPES,
    _UNSUPPORTED,
    TWO_PLACES,
    Fa3MappingError,
    InvoiceContext,
    _aggregate_vat,
    _check_form_code,
    _check_p15,
    _dec_or_none,
    _fmt2,
    _localname_tree,
    _req,
    _resolve_kurs,
    _text,
)
from app.ams_transform import encode_cp1250
from app.kontrahenci import _clean

WERSJA_SZABLONU = "4"
WERSJA_PROGRAMU = "308"

# rodzaje faktur księgowane automatycznie; ZAL/ROZ (zaliczkowe/rozliczeniowe)
# mają dekret zależny od rozliczenia zaliczek -> raport błędów, ręcznie
_RODZAJE_AUTO = {"VAT", "UPR", "KOR"}


@dataclass
class HKontrahent:
    """Karta kontrahenta pod sekcję Kontrahent{} szablonu hmffk."""

    klucz: str
    nip: str = ""
    nazwa: str = ""
    kraj: str = "PL"
    ulica: str = ""
    nr_domu: str = ""
    nr_lokalu: str = ""
    kod_pocztowy: str = ""
    miejscowosc: str = ""
    email: str = ""
    telefon: str = ""
    nazwa_banku: str = ""
    rachunek: str = ""
    osoba_fizyczna: bool = False
    imie: str = ""
    nazwisko: str = ""
    # kod karty: `kod` = uzgodniony kod karty z FK (wysyłany bez zmian, ma
    # pierwszeństwo przy unikalności); bez niego kod liczony z `nazwa_do_kodu`
    # (albo z `nazwa`), przycięty do 40 zn. — patrz _unikalne_skroty
    kod: str = ""
    nazwa_do_kodu: str = ""


@dataclass
class HRejestr:
    stawka: str
    netto: str          # PLN
    vat: str            # PLN
    brutto: str         # PLN
    netto_wal: str = ""
    vat_wal: str = ""
    brutto_wal: str = ""


@dataclass
class HDokument:
    symbol: str                 # FVS/FKS (sprzedaż) | FVZ/FKZ (zakup)
    rodzaj_dok: str             # "sprzedaży" | "zakupu"
    numer: str
    data_wystawienia: str
    data_operacji: str          # sprzedaż: data sprzedaży; zakup: data zakupu
    data_wplywu: str            # zakup: data przyjęcia w KSeF; sprzedaż: ""
    termin_platnosci: str
    kwota: str                  # brutto ogółem w PLN
    mpp: bool
    waluta: str                 # "" dla PLN
    kurs: str
    wkwota: str                 # brutto ogółem w walucie (gdy nie PLN)
    ksef_number: str
    ksef_date: str
    earchive_id: str
    kontrahent_klucz: str
    kontrahent: HKontrahent
    rejestry: list[HRejestr]
    korekty: list[tuple[str, str, str]] = field(default_factory=list)  # (nr, data, nrKSeF)
    opis: str = ""  # treść dokumentu w FK ([opis FK])
    opis_auto: bool = False  # opis = opis_dokumentu(...) — do przeliczenia po zmianie nazwy
    konto_rozrachunkowe: str = ""  # pełne konto z analityką kontrahenta (np. 201-2-23)
    # pełny dekret sprzedaży (exporter ustawia tylko dla sprzedaży): konto linii
    # MA netto i MA VAT; puste -> linia bez "Konto =" (koszt: księgowa dekretuje ręcznie)
    konto_netto: str = ""
    konto_vat: str = ""


@dataclass
class HmffkMapResult:
    dokument: HDokument
    kontrahent: HKontrahent
    warnings: list[str] = field(default_factory=list)


class KontrahenciRejestr:
    """Deduplikacja kart po stabilnym kluczu; scala pola (istniejące wygrywa,
    puste uzupełniane z nowego rekordu — np. rachunek bankowy z kolejnej faktury)."""

    def __init__(self) -> None:
        self._by_klucz: dict[str, HKontrahent] = {}

    def add(self, k: HKontrahent) -> HKontrahent:
        existing = self._by_klucz.get(k.klucz)
        if existing is None:
            self._by_klucz[k.klucz] = k
            return k
        for f in fields(HKontrahent):
            if f.name in ("klucz", "osoba_fizyczna"):
                continue
            if not getattr(existing, f.name) and getattr(k, f.name):
                setattr(existing, f.name, getattr(k, f.name))
        return existing

    def lista(self) -> list[HKontrahent]:
        return [self._by_klucz[k] for k in sorted(self._by_klucz)]

    def __len__(self) -> int:
        return len(self._by_klucz)


# --------------------------------------------------------------------------- #
# mapowanie FA(3)

def _digits(value: str | None) -> str:
    return "".join(c for c in (value or "") if c.isdigit())


def _kontrahent_z_podmiotu(podmiot, warnings: list[str], rola: str) -> HKontrahent:
    dane = podmiot.find("DaneIdentyfikacyjne")
    nip = _digits(_text(dane, "NIP"))
    nazwa = _text(dane, "Nazwa") or ""
    if not nazwa:
        warnings.append(f"{rola} bez nazwy (faktura uproszczona?) — uzupełnij kartę w FK")
    alt_id = _text(dane, "NrVatUE") or _text(dane, "NrID")

    adres = podmiot.find("Adres")
    kraj = (_text(adres, "KodKraju") or "PL").strip() or "PL"
    adres_l1 = _text(adres, "AdresL1") or ""
    adres_l2 = _text(adres, "AdresL2") or ""
    s = parse_adres(adres_l1, adres_l2)

    email, telefon = "", ""
    for dk in podmiot.findall("DaneKontaktowe"):
        email = email or (_text(dk, "Email") or "")
        telefon = telefon or (_text(dk, "Telefon") or "")

    # osoba fizyczna: krajowy podmiot bez żadnego identyfikatora podatkowego
    osoba = not nip and not alt_id and kraj == "PL"
    imie, nazwisko = "", ""
    if osoba:
        tokens = nazwa.split()
        if len(tokens) == 2:
            imie, nazwisko = tokens[0], tokens[1]
    if not nip and alt_id:
        warnings.append(
            f"{rola} z zagranicznym identyfikatorem {alt_id} — NIP UE uzupełnij na karcie w FK"
        )

    if nip:
        klucz = nip
    else:
        digest = hashlib.sha1(f"{nazwa}|{s.miejscowosc or adres_l2}".encode("utf-8"))
        klucz = "K_" + digest.hexdigest()[:12].upper()

    return HKontrahent(
        klucz=klucz, nip=nip, nazwa=nazwa, kraj=kraj,
        ulica=s.ulica or adres_l1, nr_domu=s.nr_domu, nr_lokalu=s.nr_lokalu,
        kod_pocztowy=s.kod_pocztowy, miejscowosc=s.miejscowosc,
        email=email, telefon=telefon,
        osoba_fizyczna=osoba, imie=imie, nazwisko=nazwisko,
    )


def opis_dokumentu(korekta: bool, numer: str, nazwa_kontrahenta: str) -> str:
    """Treść dokumentu w FK ([opis FK]): „Faktura <nr> - <kontrahent>”."""
    prefiks = "Korekta faktury" if korekta else "Faktura"
    return f"{prefiks} {numer} - {nazwa_kontrahenta}"[:120]


def map_fa3_to_hmffk(
    fa_xml: bytes, ctx: InvoiceContext, *, termin_fallback_dni: int = 14
) -> HmffkMapResult:
    """FA(3) -> dane dokumentu i kontrahenta pod plik hmffk. Rzuca Fa3MappingError.

    `termin_fallback_dni`: gdy FA(3) nie niesie TerminPlatnosci (na żywo dotyczyło
    to większości faktur), termin_platnosci = data wystawienia (P_1) + tyle dni —
    dzięki temu KAŻDY dokument dostaje sekcję transakcja{} (rozrachunek w FK)."""
    warnings: list[str] = []
    root = _localname_tree(fa_xml)
    if root.tag != "Faktura":
        raise Fa3MappingError(f"Nieoczekiwany element główny: {root.tag!r}")
    _check_form_code(root, warnings)

    fa = _req(root, "Fa", "brak sekcji Fa")
    podmiot1 = _req(root, "Podmiot1", "brak Podmiot1 (sprzedawca)")
    podmiot2 = _req(root, "Podmiot2", "brak Podmiot2 (nabywca)")

    numer = _text(fa, "P_2") or _err("brak numeru faktury (P_2)")
    data_wyst = _text(fa, "P_1") or _err("brak daty wystawienia (P_1)")
    kod_waluty = (_text(fa, "KodWaluty") or _err("brak kodu waluty (KodWaluty)")).upper()
    rodzaj = (_text(fa, "RodzajFaktury") or _err("brak RodzajFaktury")).upper()

    for field_name, desc in _UNSUPPORTED:
        if _dec_or_none(_text(fa, field_name)) not in (None, Decimal("0")):
            raise Fa3MappingError(
                f"Faktura z polem {field_name} ({desc}) — do ręcznego zaksięgowania"
            )
    if rodzaj not in _RODZAJE_AUTO:
        raise Fa3MappingError(
            f"Rodzaj faktury {rodzaj} (zaliczkowa/rozliczeniowa) — do ręcznego zaksięgowania"
        )

    is_pln = kod_waluty == "PLN"
    kurs = _resolve_kurs(fa, is_pln, warnings)
    buckets = _aggregate_vat(fa, is_pln, kurs, warnings)
    if not buckets:
        raise Fa3MappingError("Brak kwot podsumowania VAT (P_13_x) — do ręcznego zaksięgowania")
    _check_p15(fa, buckets, warnings, numer)

    sprzedaz = ctx.subject_type == "Subject1"
    jest_korekta = rodzaj in _KOR_TYPES
    if sprzedaz:
        symbol = "FKS" if jest_korekta else "FVS"
        rodzaj_dok = "sprzedaży"
    else:
        symbol = "FKZ" if jest_korekta else "FVZ"
        rodzaj_dok = "zakupu"

    data_oper = _text(fa, "P_6") or ""
    if not data_oper:
        okres = fa.find("OkresFa")
        if okres is not None:
            data_oper = _text(okres, "P_6_Do") or ""
    if not data_oper:
        data_oper = data_wyst

    termin = ""
    nazwa_banku, rachunek = "", ""
    platnosc = fa.find("Platnosc")
    if platnosc is not None:
        for tp in platnosc.findall("TerminPlatnosci"):
            termin = _text(tp, "Termin") or ""
            if termin:
                break
        rb = platnosc.find("RachunekBankowy")
        if rb is not None:
            rachunek = _text(rb, "NrRB") or ""
            nazwa_banku = _text(rb, "NazwaBanku") or ""
    if not termin:
        termin = (date.fromisoformat(data_wyst) + timedelta(days=termin_fallback_dni)).isoformat()

    adnotacje = fa.find("Adnotacje")
    mpp = adnotacje is not None and _text(adnotacje, "P_18A") == "1"

    rola = "Nabywca" if sprzedaz else "Sprzedawca"
    kontrahent = _kontrahent_z_podmiotu(podmiot2 if sprzedaz else podmiot1, warnings, rola)
    if not sprzedaz and rachunek and not kontrahent.rachunek:
        # rachunek sprzedawcy z faktury kosztowej -> karta kontrahenta (przelewy)
        kontrahent.rachunek = rachunek
        kontrahent.nazwa_banku = nazwa_banku

    rejestry: list[HRejestr] = []
    suma_pln = Decimal("0")
    for b in buckets:
        if is_pln:
            netto_pln, vat_pln = b.netto, b.vat
            rej = HRejestr(
                stawka=b.stawka, netto=_fmt2(netto_pln), vat=_fmt2(vat_pln),
                brutto=_fmt2(netto_pln + vat_pln),
            )
        else:
            netto_pln = (b.netto * Decimal(kurs)).quantize(TWO_PLACES)
            vat_pln = b.vat_kurs
            rej = HRejestr(
                stawka=b.stawka, netto=_fmt2(netto_pln), vat=_fmt2(vat_pln),
                brutto=_fmt2(netto_pln + vat_pln),
                netto_wal=_fmt2(b.netto), vat_wal=_fmt2(b.vat),
                brutto_wal=_fmt2(b.netto + b.vat),
            )
        suma_pln += netto_pln + vat_pln
        rejestry.append(rej)

    p15 = _dec_or_none(_text(fa, "P_15"))
    if is_pln:
        kwota = _fmt2(p15) if p15 is not None else _fmt2(suma_pln)
        wkwota = ""
    else:
        kwota = _fmt2(suma_pln)
        wkwota = _fmt2(p15) if p15 is not None else ""
        warnings.append(
            f"Faktura walutowa ({kod_waluty}, kurs {kurs}) — zweryfikuj kwoty PLN "
            "i kurs na dokumencie przed zaksięgowaniem"
        )

    korekty: list[tuple[str, str, str]] = []
    if jest_korekta:
        for dane in fa.findall("DaneFaKorygowanej"):
            nr_ksef = _text(dane, "NrKSeFFaKorygowanej") or ""
            if not nr_ksef and _text(dane, "NrKSeFN") == "1":
                warnings.append("Korygowana faktura wystawiona poza KSeF (NrKSeFN)")
            korekty.append((
                _text(dane, "NrFaKorygowanej") or "",
                _text(dane, "DataWystFaKorygowanej") or "",
                nr_ksef,
            ))
        if not korekty:
            warnings.append("Faktura korygująca bez DaneFaKorygowanej — brak odwołania do korygowanej")

    opis = opis_dokumentu(jest_korekta, numer, kontrahent.nazwa)

    dokument = HDokument(
        opis=opis,
        opis_auto=True,
        symbol=symbol, rodzaj_dok=rodzaj_dok, numer=numer,
        data_wystawienia=data_wyst, data_operacji=data_oper,
        data_wplywu="" if sprzedaz else ctx.ksef_date,
        termin_platnosci=termin, kwota=kwota, mpp=mpp,
        waluta="" if is_pln else kod_waluty, kurs="" if is_pln else kurs, wkwota=wkwota,
        ksef_number=ctx.ksef_number, ksef_date=ctx.ksef_date,
        # świeży GUID per wygenerowany plik (jak w trybie SKsef): identyfikuje
        # egzemplarz importu; kolumna w FK to uniqueidentifier
        earchive_id=str(uuid.uuid4()).upper(),
        kontrahent_klucz=kontrahent.klucz, kontrahent=kontrahent,
        rejestry=rejestry, korekty=korekty,
    )
    return HmffkMapResult(dokument=dokument, kontrahent=kontrahent, warnings=warnings)


def _err(msg: str):
    raise Fa3MappingError(msg)


# --------------------------------------------------------------------------- #
# budowa pliku

# Konwencja Symfonii dla stawek literałowych w sekcji rejestr{} szablonu hmffk.AMS
# (patrz app/fa_mapper.py:76-77, komentarz przy _ZERO_GROUPS): zwolniona (zw) to
# stawka -1, poza terytorium kraju (np) to stawka -2. $stawka=[stawka] w szablonie
# parsuje wartość liczbowo — dosłowne "stawka =zw"/"stawka =np" FK czytałby jako 0.
_STAWKA_AMS = {"zw": "-1", "np": "-2"}


def _stawka_ams(stawka: str) -> str:
    """Stawka wg konwencji Symfonii (_STAWKA_AMS) dla emisji w rejestr{}; wartości
    numeryczne (23/8/5/0 itd.) wracają bez zmian. HRejestr.stawka w danych zostaje
    źródłowa (nietłumaczona) — tłumaczenie dotyczy wyłącznie emisji do pliku."""
    return _STAWKA_AMS.get(stawka, stawka)


_WZORZEC_NUMERU = re.compile(r"(\d+)")


def _klucz_numeru(numer: str) -> tuple:
    r"""Klucz sortowania naturalnego numeru faktury: dzieli numer na przebiegi
    cyfr i nie-cyfr (re.split(r"(\d+)")) — cyfry porównywane liczbowo, tekst
    bez rozróżniania wielkości liter (str.casefold). Każdy element krotki to
    para (znacznik, wartość): (0, int) dla przebiegu cyfr, (1, str.casefold)
    dla przebiegu tekstu. Dzięki znacznikowi porównanie dwóch kluczy zawsze
    zestawia wartości tego samego typu (int z int, str z str) — również
    między numerami o zupełnie różnych formatach — więc nigdy nie dochodzi
    do TypeError (int vs str) przy sortowaniu mieszanych numeracji."""
    czesci = _WZORZEC_NUMERU.split(numer)
    return tuple(
        (0, int(cz)) if cz.isdigit() else (1, cz.casefold())
        for cz in czesci
        if cz != ""
    )


def _unikalne_skroty(kontrahenci: list[HKontrahent]) -> dict[str, str]:
    """kod (skrót) karty, ucięty do <=40 zn.

    kod = k.kod (kod karty z FK, gdy znany) albo (k.nazwa_do_kodu or
    nazwa).strip()[:40]; fallback NIP/klucz gdy nazwa pusta. Unikalność
    w pliku wymagana — sufiks ~2/~3... doklejany tylko przy realnej kolizji
    (dwie różne nazwy o identycznych pierwszych 40 znakach), z przycięciem
    bazy tak, by całość kodu nadal mieściła się w limicie 40 znaków.
    """
    used: set[str] = set()
    result: dict[str, str] = {}
    # kod z FK idzie bez zmian (nigdy sufiks — inaczej plik niósłby inny kod
    # niż karta) i jest rezerwowany pierwszy; sufiks dostaje najwyżej karta
    # z kodem liczonym
    for k in kontrahenci:
        if k.kod.strip():
            result[k.klucz] = _clean(k.kod)
            used.add(_postac_w_pliku(k.kod))
    for k in kontrahenci:
        if k.kod.strip():
            continue
        zrodlo = k.nazwa_do_kodu.strip() or k.nazwa.strip() or k.nip or k.klucz
        base = _clean(zrodlo)[:40].strip()
        kod, i = base, 2
        while _postac_w_pliku(kod) in used:
            suffix = f"~{i}"
            kod = base[: 40 - len(suffix)].strip() + suffix
            i += 1
        used.add(_postac_w_pliku(kod))
        result[k.klucz] = kod
    return result


def _postac_w_pliku(kod: str) -> str:
    """Kod tak, jak zobaczy go Symfonia: po _clean i zapisie w CP1250
    (transliteracja — „Møller” i „Moller” to w pliku ten sam kod)."""
    return encode_cp1250(_clean(kod))[0].decode("cp1250").upper()


def _linie_kontrahenta(k: HKontrahent, kod: str) -> list[str]:
    lines = [
        "Kontrahent{",
        f"id ={_clean(k.klucz)}",
        f"kod ={_clean(kod)}",
        f"nazwa ={_clean(k.nazwa)}",
    ]
    if k.nip:
        lines.append(f"nip ={_clean(k.nip)}")
    if k.ulica:
        lines.append(f"ulica ={_clean(k.ulica)}")
    if k.nr_domu:
        lines.append(f"dom ={_clean(k.nr_domu)}")
    if k.nr_lokalu:
        lines.append(f"lokal ={_clean(k.nr_lokalu)}")
    if k.kod_pocztowy:
        lines.append(f"kodPocztowy ={k.kod_pocztowy}")
    if k.miejscowosc:
        lines.append(f"miejscowosc ={_clean(k.miejscowosc)}")
    if k.email:
        lines.append(f"email ={_clean(k.email)}")
    if k.telefon:
        lines.append(f"telefon1 ={_clean(k.telefon)}")
    if k.nazwa_banku:
        lines.append(f"nazwaBanku ={_clean(k.nazwa_banku)}")
    if k.rachunek:
        lines.append(f"numerRachunku ={_clean(k.rachunek)}")
    if k.osoba_fizyczna:
        lines.append("osfiz =1")
    if k.imie:
        lines.append(f"imie ={_clean(k.imie)}")
    if k.nazwisko:
        lines.append(f"nazwisko ={_clean(k.nazwisko)}")
    lines += [
        "aktywny =1",
        "kraj{",
        f"symbol ={_clean(k.kraj)}",
        "}",
        "}",
        "",
    ]
    return lines


def _linie_dokumentu(d: HDokument, kod_kontrahenta: str) -> list[str]:
    k = d.kontrahent
    lines = [
        "dokument{",
        f"obsluguj jak ={d.symbol}",
        f"symbol FK ={d.symbol}",
        f"FK nazwa ={_clean(d.numer)}",
        f"rodzaj_dok ={d.rodzaj_dok}",
        "naliczenie_VAT =0",
    ]
    if d.opis:
        lines.append(f"opis FK ={_clean(d.opis)}")
    if d.rodzaj_dok == "zakupu":
        lines += [
            f"dataWystawieniaObca ={d.data_wystawienia}",
            f"dataZakupu ={d.data_operacji}",
            f"Datawpl ={d.data_wplywu}",
        ]
    else:
        lines += [
            f"dataWystawienia ={d.data_wystawienia}",
            f"dataSprzedazy ={d.data_operacji}",
        ]
    if d.termin_platnosci:
        lines.append(f"terminPlatnosci ={d.termin_platnosci}")
    lines.append(f"kwota ={d.kwota}")
    if d.mpp:
        lines.append("mppFlags =1")
    if d.waluta:
        lines += [
            f"waluta ={d.waluta}",
            f"kurs ={d.kurs}",
        ]
        if d.wkwota:
            lines.append(f"wkwota ={d.wkwota}")
    lines += [
        f"KsefNumber ={d.ksef_number}",
        f"KSeFDate ={d.ksef_date}",
        f"eArchiveId ={d.earchive_id}",
    ]

    sekcja_kh = "Dane nabywcy{" if d.rodzaj_dok == "sprzedaży" else "Dane sprzedawcy{"
    lines += [sekcja_kh, f"khid ={_clean(d.kontrahent_klucz)}"]
    if k.nip:
        lines.append(f"khnip ={_clean(k.nip)}")
    lines.append(f"khnazwa ={_clean(k.nazwa)}")
    if k.miejscowosc:
        lines.append(f"khmiasto ={_clean(k.miejscowosc)}")
    if k.ulica:
        lines.append(f"KhUlica ={_clean(k.ulica)}")
    if k.nr_domu:
        lines.append(f"KhDomu ={_clean(k.nr_domu)}")
    if k.nr_lokalu:
        lines.append(f"KhLokal ={_clean(k.nr_lokalu)}")
    if k.kod_pocztowy:
        lines.append(f"khkodpocz ={k.kod_pocztowy}")
    lines += [
        "fk_ident =0",
        "kraj{",
        f"symbol ={_clean(k.kraj)}",
        "}",
        "}",
    ]

    # okres rejestru: sprzedaż -> data sprzedaży (obowiązek podatkowy);
    # zakup -> data wpływu z KSeF (moment prawa do odliczenia)
    okres = d.data_operacji if d.rodzaj_dok == "sprzedaży" else d.data_wplywu
    for r in d.rejestry:
        lines += [
            "rejestr{",
            f"okres ={okres}",
            f"netto ={r.netto}",
            f"vat ={r.vat}",
            f"brutto ={r.brutto}",
            f"stawka ={_stawka_ams(r.stawka)}",
        ]
        if d.rodzaj_dok == "sprzedaży":
            lines.append("metoda_vat =0")
        if r.netto_wal:
            lines += [
                f"nettoWaluta ={r.netto_wal}",
                f"vatWaluta ={r.vat_wal}",
                f"bruttoWaluta ={r.brutto_wal}",
            ]
        lines.append("}")

    for nr, data, nr_ksef in d.korekty:
        lines += [
            "dokument korygowany{",
            f"kod ={_clean(nr)}",
            f"data ={data}",
        ]
        if nr_ksef:
            lines.append(f"numerKsef ={nr_ksef}")
        lines.append("}")

    lines += _linie_zapisow(d)
    lines += ["}", ""]
    return lines


def _linie_zapisow(d: HDokument) -> list[str]:
    """Szkielet dekretu: strony i kwoty. Każda linia dostaje "Konto =" (zaraz po
    "kwota =", przed liniami walutowymi) wyłącznie gdy odpowiadające pole dokumentu
    jest niepuste — linia brutto z d.konto_rozrachunkowe (konto rozrachunkowe
    kontrahenta, "<prefiks>-<pozycja>"), linia netto z d.konto_netto, linia VAT
    z d.konto_vat (pełny dekret sprzedaży: exporter ustawia je tylko dla dokumentów
    sprzedaży — dla kosztu zostają puste i konta kosztowe dekretuje księgowa
    ręcznie w FK). Linia zerowej kwoty w ogóle się nie pojawia.
    Sprzedaż: WN brutto / MA netto / MA VAT; zakup: WN netto / MA brutto / WN VAT
    (kolejność jak w oryginalnym dekrecie zakupu Symfonii).

    Rozrachunki (reference/rozrachunki-hmffk-E2E.md): linia brutto to zapis
    ROZRACHUNKOWY (sprzedaż WN, zakup MA) — gdy ma konto, dostaje dodatkowo
    "NumerDok =" i "IdDlaRozliczen =1"; parowanie z sekcją transakcja{} w FK
    działa WYŁĄCZNIE po zgodności tego identyfikatora. Gdy dokument ma
    termin_platnosci (ISO, bez konwersji — inny format FK czyta jako liczbę
    dni), zaraz po zamknięciu tego zapisu dochodzi sekcja transakcja{} z tym
    samym terminem i kwotą brutto. Linie netto/VAT nigdy nie dostają tych pól."""
    netto = sum(Decimal(r.netto) for r in d.rejestry)
    vat = sum(Decimal(r.vat) for r in d.rejestry)
    brutto = netto + vat
    if d.waluta:
        netto_w = sum(Decimal(r.netto_wal) for r in d.rejestry if r.netto_wal)
        vat_w = sum(Decimal(r.vat_wal) for r in d.rejestry if r.vat_wal)
        brutto_w = netto_w + vat_w
    else:
        netto_w = vat_w = brutto_w = None

    if d.rodzaj_dok == "sprzedaży":
        pozycje = [
            ("WN", brutto, brutto_w, "brutto"),
            ("MA", netto, netto_w, "netto"),
            ("MA", vat, vat_w, "vat"),
        ]
    else:
        # kolejność jak w oryginalnym dekrecie zakupu Symfonii (XSLT): netto,
        # brutto (linia rozrachunkowa — MA, tu też Konto/NumerDok/transakcja
        # gdy dotyczy), na końcu VAT
        pozycje = [
            ("WN", netto, netto_w, "netto"),
            ("MA", brutto, brutto_w, "brutto"),
            ("WN", vat, vat_w, "vat"),
        ]
    konta = {"brutto": d.konto_rozrachunkowe, "netto": d.konto_netto, "vat": d.konto_vat}

    lines: list[str] = []
    for strona, kwota, kwota_w, rodzaj_linii in pozycje:
        if kwota == 0:
            continue
        lines += ["zapis{", f"strona ={strona}", f"kwota ={_fmt2(kwota)}"]
        konto = konta[rodzaj_linii]
        rozrachunkowy = rodzaj_linii == "brutto" and bool(konto)
        if konto:
            lines.append(f"Konto ={_clean(konto)}")
        if rozrachunkowy:
            lines.append(f"NumerDok ={_clean(d.numer)}")
            lines.append("IdDlaRozliczen =1")
        if d.waluta and kwota_w is not None:
            lines += [
                f"wkwota ={_fmt2(kwota_w)}",
                f"waluta ={d.waluta}",
                f"kurs ={d.kurs}",
            ]
        lines.append("}")
        if rozrachunkowy and d.termin_platnosci:
            lines += [
                "transakcja{",
                "idDlaRozliczen =1",
                f"termin ={d.termin_platnosci}",
                f"kwota ={_fmt2(kwota)}",
                "}",
            ]
    return lines


def build_hmffk_text(kontrahenci: list[HKontrahent], dokumenty: list[HDokument]) -> str:
    """JEDEN plik: INFO{} + wszystkie Kontrahent{} + wszystkie dokument{} (LF;
    CP1250+CRLF nakłada zapis — jak w ams_transform). Dokumenty sortowane
    NATURALNIE po numerze faktury jako kluczu głównym (_klucz_numeru — przebiegi
    cyfr porównywane liczbowo, więc "FV/2/2026" wypada przed "FV/10/2026", a nie
    odwrotnie jak przy sortowaniu leksykograficznym; data_wystawienia jako klucz
    pomocniczy przy remisach numeru)."""
    skroty = _unikalne_skroty(kontrahenci)
    lines: list[str] = [
        "INFO{",
        f"Wersja szablonu ={WERSJA_SZABLONU}",
        f"Wersja_programu ={WERSJA_PROGRAMU}",
        "}",
        "",
    ]
    for k in kontrahenci:
        lines += _linie_kontrahenta(k, skroty[k.klucz])
    for d in sorted(dokumenty, key=lambda x: (_klucz_numeru(x.numer), x.data_wystawienia)):
        lines += _linie_dokumentu(d, skroty.get(d.kontrahent_klucz, ""))
    return "\n".join(lines) + "\n"
