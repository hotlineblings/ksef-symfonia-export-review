"""Testy trybu hmffk: FA(3) -> jeden plik AMS "format FK 3.0" pod hmffk.AMS."""

from pathlib import Path

import pytest

from app.ams_transform import encode_cp1250
from app.fa_mapper import Fa3MappingError, InvoiceContext
from app.hmffk import (
    HDokument,
    HKontrahent,
    KontrahenciRejestr,
    _klucz_numeru,
    _unikalne_skroty,
    build_hmffk_text,
    map_fa3_to_hmffk,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"

CTX_SPRZEDAZ = InvoiceContext(
    ksef_number="5252445719-20260115-A1B2C3D4E5F6-7A",
    ksef_date="2026-01-16",
    subject_type="Subject1",
)
CTX_KOSZT = InvoiceContext(
    ksef_number="5252445719-20260115-A1B2C3D4E5F6-7A",
    ksef_date="2026-01-16",
    subject_type="Subject2",
)


def _load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _mutate(name: str, old: str, new: str) -> bytes:
    data = _load(name)
    assert old.encode() in data
    return data.replace(old.encode(), new.encode())


def test_sprzedaz_basic():
    res = map_fa3_to_hmffk(_load("fa3_basic_23.xml"), CTX_SPRZEDAZ)
    d = res.dokument
    assert d.symbol == "FVS"
    assert d.rodzaj_dok == "sprzedaży"
    assert d.data_wystawienia == "2026-01-15"
    assert d.data_operacji == "2026-01-14"      # P_6
    assert d.data_wplywu == ""                  # sprzedaż: bez daty wpływu
    assert d.kwota == "1230.00"
    assert d.waluta == "" and d.wkwota == ""
    assert d.ksef_number == CTX_SPRZEDAZ.ksef_number
    assert d.ksef_date == "2026-01-16"
    assert len(d.rejestry) == 1
    r = d.rejestry[0]
    assert (r.stawka, r.netto, r.vat, r.brutto) == ("23", "1000.00", "230.00", "1230.00")
    # kontrahent faktury sprzedaży = nabywca (Podmiot2), klucz stabilny = NIP
    k = res.kontrahent
    assert k.nip == "5252445719"
    assert k.klucz == "5252445719"
    assert d.kontrahent_klucz == k.klucz
    assert (k.ulica, k.nr_domu, k.kod_pocztowy, k.miejscowosc) == (
        "Nasza", "1", "02-002", "Warszawa"
    )
    assert not k.osoba_fizyczna


def test_koszt_basic():
    res = map_fa3_to_hmffk(_load("fa3_basic_23.xml"), CTX_KOSZT)
    d = res.dokument
    assert d.symbol == "FVZ"
    assert d.rodzaj_dok == "zakupu"
    assert d.data_wplywu == "2026-01-16"        # data przyjęcia w KSeF
    # kontrahent faktury kosztowej = sprzedawca (Podmiot1)
    k = res.kontrahent
    assert k.nip == "1180012345"
    assert (k.ulica, k.nr_domu, k.nr_lokalu) == ("Przykladowa", "12", "3")
    assert k.email == "biuro@dostawca-test.pl"


def test_multi_rate():
    res = map_fa3_to_hmffk(_load("fa3_multi_rate.xml"), CTX_SPRZEDAZ)
    got = {r.stawka: (r.netto, r.vat, r.brutto) for r in res.dokument.rejestry}
    assert got == {
        "23": ("1000.00", "230.00", "1230.00"),
        "8": ("200.00", "16.00", "216.00"),
        "zw": ("50.00", "0.00", "50.00"),
    }
    assert res.dokument.kwota == "1496.00"


# --------------------------------------------------------------------------- #
# stawka "zw"/"np" w emisji rejestr{} (konwencja Symfonii: zw -> -1, np -> -2 —
# patrz app/fa_mapper.py:76-77 i app/hmffk.py::_stawka_ams). Dotyczy wyłącznie
# tekstu pliku AMS; HRejestr.stawka w danych zostaje źródłowa ("zw"/"np").

def test_stawka_zw_w_pliku_ams_jako_minus_jeden():
    res = map_fa3_to_hmffk(_load("fa3_multi_rate.xml"), CTX_SPRZEDAZ)
    assert any(r.stawka == "zw" for r in res.dokument.rejestry)  # dane źródłowe: "zw"
    text = build_hmffk_text([res.kontrahent], [res.dokument])
    assert "stawka =-1" in text
    assert "stawka =zw" not in text


def test_stawka_np_w_pliku_ams_jako_minus_dwa():
    data = _mutate("fa3_basic_23.xml", "<P_13_1>1000.00</P_13_1>", "<P_13_8>1000.00</P_13_8>")
    data = data.replace(b"<P_14_1>230.00</P_14_1>", b"")
    data = data.replace(b"<P_15>1230.00</P_15>", b"<P_15>1000.00</P_15>")
    res = map_fa3_to_hmffk(data, CTX_SPRZEDAZ)
    assert res.dokument.rejestry[0].stawka == "np"  # dane źródłowe: "np"
    text = build_hmffk_text([res.kontrahent], [res.dokument])
    assert "stawka =-2" in text
    assert "stawka =np" not in text


def test_stawka_numeryczna_bez_zmian():
    res = map_fa3_to_hmffk(_load("fa3_basic_23.xml"), CTX_SPRZEDAZ)
    text = build_hmffk_text([res.kontrahent], [res.dokument])
    assert "stawka =23" in text


def test_kor():
    res = map_fa3_to_hmffk(_load("fa3_kor.xml"), CTX_SPRZEDAZ)
    d = res.dokument
    assert d.symbol == "FKS"
    assert d.rodzaj_dok == "sprzedaży"
    assert d.data_operacji == "2026-03-05"      # brak P_6 -> data wystawienia
    assert d.rejestry[0].netto == "-100.00"
    assert d.kwota == "-123.00"
    assert d.korekty == [
        ("FV/2026/01/0007", "2026-01-15", "1180012345-20260115-A1B2C3D4E5F6-7A")
    ]


def test_kor_koszt_symbol():
    res = map_fa3_to_hmffk(_load("fa3_kor.xml"), CTX_KOSZT)
    assert res.dokument.symbol == "FKZ"


def test_eur_kwoty_pln_i_waluta():
    res = map_fa3_to_hmffk(_load("fa3_eur.xml"), CTX_KOSZT)
    d = res.dokument
    assert d.waluta == "EUR" and d.kurs == "4.3"
    assert d.wkwota == "1230.00"                # P_15 w walucie
    r = d.rejestry[0]
    assert r.netto == "4300.00"                 # 1000 EUR x 4.3
    assert r.vat == "989.00"                    # P_14_1W (PLN)
    assert r.brutto == "5289.00"
    assert (r.netto_wal, r.vat_wal, r.brutto_wal) == ("1000.00", "230.00", "1230.00")
    assert d.kwota == "5289.00"                 # brutto ogółem w PLN
    assert any("walutowa" in w for w in res.warnings)


def test_nabywca_unijny_bez_nip():
    res = map_fa3_to_hmffk(_load("fa3_no_nip.xml"), CTX_SPRZEDAZ)
    k = res.kontrahent
    assert k.nip == ""
    assert k.klucz.startswith("K_")
    assert k.kraj == "DE"
    assert not k.osoba_fizyczna                 # firma zagraniczna, nie os. fizyczna
    assert any("zagranicznym identyfikatorem" in w for w in res.warnings)
    assert res.dokument.rejestry[0].stawka == "0"


def test_osoba_fizyczna_bez_nip():
    data = _mutate("fa3_basic_23.xml", "<NIP>5252445719</NIP>", "")
    data = data.replace(b"Nasza Firma Sp. z o.o.", "Jan Kowalski".encode())
    res = map_fa3_to_hmffk(data, CTX_SPRZEDAZ)
    k = res.kontrahent
    assert k.osoba_fizyczna
    assert (k.imie, k.nazwisko) == ("Jan", "Kowalski")
    assert k.klucz.startswith("K_")


def test_zaliczkowa_odrzucona():
    data = _mutate(
        "fa3_basic_23.xml", "<RodzajFaktury>VAT</RodzajFaktury>", "<RodzajFaktury>ZAL</RodzajFaktury>"
    )
    with pytest.raises(Fa3MappingError, match="ZAL"):
        map_fa3_to_hmffk(data, CTX_SPRZEDAZ)


def test_marza_odrzucona():
    data = _mutate(
        "fa3_basic_23.xml", "<P_15>1230.00</P_15>", "<P_13_11>500.00</P_13_11><P_15>1230.00</P_15>"
    )
    with pytest.raises(Fa3MappingError, match="marży"):
        map_fa3_to_hmffk(data, CTX_SPRZEDAZ)


def test_earchiveid_swiezy_guid():
    a = map_fa3_to_hmffk(_load("fa3_basic_23.xml"), CTX_SPRZEDAZ).dokument.earchive_id
    b = map_fa3_to_hmffk(_load("fa3_basic_23.xml"), CTX_SPRZEDAZ).dokument.earchive_id
    assert a != b and len(a) == 36


def test_rejestr_kontrahentow_dedup_i_scalanie():
    rej = KontrahenciRejestr()
    a = map_fa3_to_hmffk(_load("fa3_basic_23.xml"), CTX_SPRZEDAZ)
    b = map_fa3_to_hmffk(_load("fa3_multi_rate.xml"), CTX_SPRZEDAZ)
    rej.add(a.kontrahent)
    merged = rej.add(b.kontrahent)
    assert len(rej) == 1                        # ten sam NIP -> jedna karta
    assert merged.email == "ksiegowosc@nasza-firma.pl"  # z 1. faktury (multi_rate bez kontaktu)


def test_build_file_struktura():
    rej = KontrahenciRejestr()
    docs = []
    for name in ("fa3_basic_23.xml", "fa3_multi_rate.xml"):
        res = map_fa3_to_hmffk(_load(name), CTX_SPRZEDAZ)
        rej.add(res.kontrahent)
        docs.append(res.dokument)
    text = build_hmffk_text(rej.lista(), docs)

    assert text.startswith("INFO{\nWersja szablonu =4\nWersja_programu =308\n}")
    assert text.count("Kontrahent{") == 1       # dedup: 1 karta dla 2 faktur
    assert text.count("dokument{") == 2
    assert text.count("rejestr{") == 4          # 1 + 3 stawki
    assert text.count("{") == text.count("}")   # zbalansowane sekcje
    assert text.index("Kontrahent{") < text.index("dokument{")
    assert "id =5252445719" in text
    assert text.count("khid =5252445719") == 2
    assert "rodzaj_dok =sprzedaży" in text
    assert "obsluguj jak =FVS" in text
    assert "metoda_vat =0" in text
    # treść dokumentu + szkielet dekretu (WN brutto / MA netto / MA VAT)
    assert "opis FK =Faktura FV/2026/01/0007 - Nasza Firma Sp. z o.o." in text
    assert text.count("zapis{") == 6  # 2 dokumenty x 3 zapisy
    assert "kwota =1230.00" in text and "kwota =1000.00" in text and "kwota =230.00" in text
    assert "KsefNumber =5252445719-20260115-A1B2C3D4E5F6-7A" in text
    # dokumenty posortowane po numerze faktury (tu zgodny z porządkiem dat)
    assert text.index("FK nazwa =FV/2026/01/0007") < text.index("FK nazwa =FV/2026/02/0031")
    # całość koduje się do CP1250 bez strat (w tym "sprzedaży")
    _, bad = encode_cp1250(text)
    assert bad == []


def test_build_file_sortowanie_po_numerze_gdy_odwrotne_do_daty():
    """Dokumenty sortowane po numerze faktury jako kluczu głównym (data_wystawienia
    to tylko klucz pomocniczy przy remisach numeru). Tu numer "A/1" ma datę
    PÓŹNIEJSZĄ niż "B/2" — mimo to "A/1" musi wystąpić w tekście przed "B/2",
    bo porządek numeryczny jest odwrotny do porządku dat."""
    data_a = _mutate("fa3_basic_23.xml", "<P_2>FV/2026/01/0007</P_2>", "<P_2>A/1</P_2>")
    data_a = data_a.replace(b"<P_1>2026-01-15</P_1>", b"<P_1>2026-07-20</P_1>")
    res_a = map_fa3_to_hmffk(data_a, CTX_SPRZEDAZ)

    data_b = _mutate("fa3_basic_23.xml", "<P_2>FV/2026/01/0007</P_2>", "<P_2>B/2</P_2>")
    data_b = data_b.replace(b"<P_1>2026-01-15</P_1>", b"<P_1>2026-07-10</P_1>")
    res_b = map_fa3_to_hmffk(data_b, CTX_SPRZEDAZ)

    assert res_a.dokument.numer == "A/1" and res_a.dokument.data_wystawienia == "2026-07-20"
    assert res_b.dokument.numer == "B/2" and res_b.dokument.data_wystawienia == "2026-07-10"

    rej = KontrahenciRejestr()
    rej.add(res_a.kontrahent)
    rej.add(res_b.kontrahent)
    # kolejność wejściowa dokumentów celowo odwrotna do oczekiwanej (po dacie: B przed A)
    text = build_hmffk_text(rej.lista(), [res_b.dokument, res_a.dokument])

    assert text.index("FK nazwa =A/1") < text.index("FK nazwa =B/2")


def test_build_file_koszt_daty_i_sekcja_sprzedawcy():
    res = map_fa3_to_hmffk(_load("fa3_basic_23.xml"), CTX_KOSZT)
    text = build_hmffk_text([res.kontrahent], [res.dokument])
    assert "rodzaj_dok =zakupu" in text
    assert "dataWystawieniaObca =2026-01-15" in text
    assert "dataZakupu =2026-01-14" in text
    assert "Datawpl =2026-01-16" in text
    assert "Dane sprzedawcy{" in text
    assert "dataSprzedazy" not in text
    # okres rejestru zakupu = data wpływu (moment odliczenia VAT)
    assert "okres =2026-01-16" in text
    # dekret zakupu: WN netto / WN VAT / MA brutto
    assert text.count("strona =WN") == 2
    assert text.count("strona =MA") == 1


def test_build_file_korekta_sekcja():
    res = map_fa3_to_hmffk(_load("fa3_kor.xml"), CTX_SPRZEDAZ)
    text = build_hmffk_text([res.kontrahent], [res.dokument])
    assert "obsluguj jak =FKS" in text
    assert "dokument korygowany{" in text
    assert "kod =FV/2026/01/0007" in text
    assert "numerKsef =1180012345-20260115-A1B2C3D4E5F6-7A" in text


def _zapis_bloki(text: str) -> list[list[str]]:
    """Zawartość kolejnych sekcji zapis{...} (linie między nawiasami klamrowymi)."""
    linie = text.splitlines()
    bloki: list[list[str]] = []
    i = 0
    while i < len(linie):
        if linie[i] == "zapis{":
            j = i + 1
            blok: list[str] = []
            while linie[j] != "}":
                blok.append(linie[j])
                j += 1
            bloki.append(blok)
            i = j + 1
        else:
            i += 1
    return bloki


def test_konto_rozrachunkowe_sprzedaz_linia_wn_brutto():
    res = map_fa3_to_hmffk(_load("fa3_basic_23.xml"), CTX_SPRZEDAZ)
    d = res.dokument
    d.konto_rozrachunkowe = "201-1-31"
    text = build_hmffk_text([res.kontrahent], [d])
    assert text.count("Konto =201-1-31") == 1
    bloki = _zapis_bloki(text)
    blok_brutto = next(b for b in bloki if "kwota =1230.00" in b)
    assert "strona =WN" in blok_brutto
    assert "Konto =201-1-31" in blok_brutto
    for b in bloki:
        if b is not blok_brutto:
            assert "Konto =" not in "\n".join(b)


def test_konto_rozrachunkowe_zakup_linia_ma_brutto():
    res = map_fa3_to_hmffk(_load("fa3_basic_23.xml"), CTX_KOSZT)
    d = res.dokument
    d.konto_rozrachunkowe = "201-1-31"
    text = build_hmffk_text([res.kontrahent], [d])
    assert text.count("Konto =201-1-31") == 1
    bloki = _zapis_bloki(text)
    blok_brutto = next(b for b in bloki if "kwota =1230.00" in b)
    assert "strona =MA" in blok_brutto
    assert "Konto =201-1-31" in blok_brutto
    for b in bloki:
        if b is not blok_brutto:
            assert "Konto =" not in "\n".join(b)


def test_bez_konta_rozrachunkowego_brak_linii_konto():
    res = map_fa3_to_hmffk(_load("fa3_basic_23.xml"), CTX_SPRZEDAZ)
    text = build_hmffk_text([res.kontrahent], [res.dokument])
    assert "Konto =" not in text


def test_pelny_dekret_sprzedazy_trzy_konta():
    """Sprzedaż: WN brutto (konto rozrachunkowe), MA netto (703), MA VAT (222-1) —
    wszystkie trzy linie w jednym dokumencie."""
    res = map_fa3_to_hmffk(_load("fa3_basic_23.xml"), CTX_SPRZEDAZ)
    d = res.dokument
    d.konto_rozrachunkowe = "201-2-31"
    d.konto_netto = "703"
    d.konto_vat = "222-1"
    text = build_hmffk_text([res.kontrahent], [d])

    bloki = _zapis_bloki(text)
    blok_brutto = next(b for b in bloki if "strona =WN" in b and "kwota =1230.00" in b)
    blok_netto = next(b for b in bloki if "strona =MA" in b and "kwota =1000.00" in b)
    blok_vat = next(b for b in bloki if "strona =MA" in b and "kwota =230.00" in b)
    assert "Konto =201-2-31" in blok_brutto
    assert "Konto =703" in blok_netto
    assert "Konto =222-1" in blok_vat
    assert text.count("Konto =") == 3


def test_koszt_bez_dekretu_netto_vat_tylko_brutto():
    """Koszt: TYLKO MA brutto dostaje konto — konto_netto/konto_vat exporter
    zostawia puste dla kosztu, więc linie netto/VAT są bez "Konto =" (dekretuje
    księgowa ręcznie w FK)."""
    res = map_fa3_to_hmffk(_load("fa3_basic_23.xml"), CTX_KOSZT)
    d = res.dokument
    d.konto_rozrachunkowe = "202-2-7"
    # d.konto_netto / d.konto_vat celowo puste (jak ustawia exporter dla kosztu)
    text = build_hmffk_text([res.kontrahent], [d])

    bloki = _zapis_bloki(text)
    blok_brutto = next(b for b in bloki if "strona =MA" in b and "kwota =1230.00" in b)
    assert "Konto =202-2-7" in blok_brutto
    assert text.count("Konto =") == 1
    for b in bloki:
        if b is not blok_brutto:
            assert "Konto =" not in "\n".join(b)


def test_koszt_kolejnosc_zapisow_netto_brutto_vat():
    """Dekret zakupu: WN netto (bez konta) -> MA brutto (Konto/NumerDok/
    IdDlaRozliczen, transakcja{} zaraz po) -> WN vat — dokładnie jak w
    oryginalnym dekrecie zakupu Symfonii (nie [WN netto, WN vat, MA brutto])."""
    res = map_fa3_to_hmffk(_load("fa3_basic_23.xml"), CTX_KOSZT)
    d = res.dokument
    d.konto_rozrachunkowe = "202-2-7"
    d.termin_platnosci = "2026-02-14"
    text = build_hmffk_text([res.kontrahent], [d])

    oczekiwany = (
        "zapis{\n"
        "strona =WN\n"
        "kwota =1000.00\n"
        "}\n"
        "zapis{\n"
        "strona =MA\n"
        "kwota =1230.00\n"
        "Konto =202-2-7\n"
        "NumerDok =FV/2026/01/0007\n"
        "IdDlaRozliczen =1\n"
        "}\n"
        "transakcja{\n"
        "idDlaRozliczen =1\n"
        "termin =2026-02-14\n"
        "kwota =1230.00\n"
        "}\n"
        "zapis{\n"
        "strona =WN\n"
        "kwota =230.00\n"
        "}\n"
    )
    assert oczekiwany in text


# --------------------------------------------------------------------------- #
# rozrachunki: NumerDok/IdDlaRozliczen na zapisie rozrachunkowym + transakcja{}
# (przepis: reference/rozrachunki-hmffk-E2E.md)

def test_rozrachunek_sprzedaz_z_terminem_platnosci():
    res = map_fa3_to_hmffk(_load("fa3_basic_23.xml"), CTX_SPRZEDAZ)
    d = res.dokument
    d.konto_rozrachunkowe = "201-2-31"
    d.termin_platnosci = "2026-01-30"
    text = build_hmffk_text([res.kontrahent], [d])

    oczekiwany = (
        "zapis{\n"
        "strona =WN\n"
        "kwota =1230.00\n"
        "Konto =201-2-31\n"
        "NumerDok =FV/2026/01/0007\n"
        "IdDlaRozliczen =1\n"
        "}\n"
        "transakcja{\n"
        "idDlaRozliczen =1\n"
        "termin =2026-01-30\n"
        "kwota =1230.00\n"
        "}\n"
    )
    assert oczekiwany in text
    assert text.count("transakcja{") == 1
    # linie netto/VAT nie dostają NumerDok/IdDlaRozliczen
    bloki = _zapis_bloki(text)
    for blok in bloki:
        if "strona =WN" in blok and "kwota =1230.00" in blok:
            continue
        polaczone = "\n".join(blok)
        assert "NumerDok" not in polaczone
        assert "IdDlaRozliczen" not in polaczone


def test_rozrachunek_zakup_z_terminem_platnosci():
    res = map_fa3_to_hmffk(_load("fa3_basic_23.xml"), CTX_KOSZT)
    d = res.dokument
    d.konto_rozrachunkowe = "202-2-7"
    d.termin_platnosci = "2026-02-14"
    text = build_hmffk_text([res.kontrahent], [d])

    oczekiwany = (
        "zapis{\n"
        "strona =MA\n"
        "kwota =1230.00\n"
        "Konto =202-2-7\n"
        "NumerDok =FV/2026/01/0007\n"
        "IdDlaRozliczen =1\n"
        "}\n"
        "transakcja{\n"
        "idDlaRozliczen =1\n"
        "termin =2026-02-14\n"
        "kwota =1230.00\n"
        "}\n"
    )
    assert oczekiwany in text
    assert text.count("transakcja{") == 1


def test_rozrachunek_bez_terminu_platnosci_brak_transakcji():
    """Bez termin_platnosci (tu: wyzerowany ręcznie na poziomie dokumentu — przez
    fallback w map_fa3_to_hmffk normalnie zawsze coś tam jest): zapis rozrachunkowy
    ma i tak NumerDok/IdDlaRozliczen (konto niepuste), ale sekcja transakcja{}
    się nie pojawia."""
    res = map_fa3_to_hmffk(_load("fa3_basic_23.xml"), CTX_SPRZEDAZ)
    d = res.dokument
    d.konto_rozrachunkowe = "201-2-31"
    d.termin_platnosci = ""
    text = build_hmffk_text([res.kontrahent], [d])

    bloki = _zapis_bloki(text)
    blok_brutto = next(b for b in bloki if "strona =WN" in b and "kwota =1230.00" in b)
    assert "NumerDok =FV/2026/01/0007" in blok_brutto
    assert "IdDlaRozliczen =1" in blok_brutto
    assert "transakcja{" not in text


def test_rozrachunek_bez_konta_brak_numerdok_i_transakcji():
    """Bez konta rozrachunkowego: żadnych pól rozrachunkowych, nawet z terminem
    płatności ustawionym — zapis idzie bez konta i bez transakcji."""
    res = map_fa3_to_hmffk(_load("fa3_basic_23.xml"), CTX_SPRZEDAZ)
    d = res.dokument
    d.termin_platnosci = "2026-01-30"
    text = build_hmffk_text([res.kontrahent], [d])
    assert "NumerDok" not in text
    assert "IdDlaRozliczen" not in text
    assert "transakcja{" not in text


# --------------------------------------------------------------------------- #
# termin_fallback_dni: brak TerminPlatnosci w FA(3) -> termin i tak powstaje
# (na żywo dotyczyło to 505/547 faktur — bez tego żaden rozrachunek by nie powstał)

def test_termin_fallback_domyslne_14_dni():
    res = map_fa3_to_hmffk(_load("fa3_basic_23.xml"), CTX_SPRZEDAZ)
    d = res.dokument
    assert d.data_wystawienia == "2026-01-15"
    assert d.termin_platnosci == "2026-01-29"  # P_1 + 14 dni (fixture bez TerminPlatnosci)

    d.konto_rozrachunkowe = "201-2-31"
    text = build_hmffk_text([res.kontrahent], [d])
    assert text.count("transakcja{") == 1
    assert "termin =2026-01-29" in text


def test_termin_fallback_parametr_zero_dni():
    res = map_fa3_to_hmffk(_load("fa3_basic_23.xml"), CTX_SPRZEDAZ, termin_fallback_dni=0)
    assert res.dokument.termin_platnosci == res.dokument.data_wystawienia == "2026-01-15"


def test_termin_ze_zrodla_ma_pierwszenstwo_nad_fallbackiem():
    """Gdy FA(3) niesie TerminPlatnosci — wartość źródłowa, fallback się nie stosuje."""
    data = _mutate(
        "fa3_basic_23.xml", "</Fa>",
        "<Platnosc><TerminPlatnosci><Termin>2026-02-01</Termin></TerminPlatnosci></Platnosc></Fa>",
    )
    res = map_fa3_to_hmffk(data, CTX_SPRZEDAZ)
    assert res.dokument.termin_platnosci == "2026-02-01"


def test_skroty_unikalne():
    rej = KontrahenciRejestr()
    a = map_fa3_to_hmffk(_load("fa3_basic_23.xml"), CTX_SPRZEDAZ)
    rej.add(a.kontrahent)
    # drugi kontrahent o tej samej nazwie, innym NIP -> skróty muszą się różnić
    data = _mutate("fa3_basic_23.xml", "<NIP>5252445719</NIP>", "<NIP>9100010362</NIP>")
    b = map_fa3_to_hmffk(data, CTX_SPRZEDAZ)
    rej.add(b.kontrahent)
    text = build_hmffk_text(rej.lista(), [a.dokument, b.dokument])
    kody = [line.split("=", 1)[1] for line in text.splitlines() if line.startswith("kod =")]
    assert len(kody) == 2 and len(set(kody)) == 2


def test_skrot_nazwa_45_znakow_ucinana_do_40():
    """Kod karty ma zawsze być jednolity z nazwą — ucięty do limitu pola FK (40 zn.)."""
    nazwa = "Kontrahent Testowy " + "X" * 26  # 45 znaków
    assert len(nazwa) == 45
    k = HKontrahent(klucz="1", nazwa=nazwa)
    skroty = _unikalne_skroty([k])
    assert skroty["1"] == nazwa[:40]
    assert len(skroty["1"]) == 40


def test_skrot_kolizja_pierwszych_40_znakow_dostaje_sufiks():
    """Dwie różne nazwy o identycznych pierwszych 40 znakach -> kolizja kodów,
    drugi kontrahent dostaje sufiks ~2, a kod nadal mieści się w limicie 40 zn."""
    wspolny_prefiks = "A" * 40
    k1 = HKontrahent(klucz="1", nazwa=wspolny_prefiks)
    k2 = HKontrahent(klucz="2", nazwa=wspolny_prefiks + " Oddział Warszawa")
    skroty = _unikalne_skroty([k1, k2])
    assert skroty["1"] == wspolny_prefiks
    assert skroty["2"] == "A" * 38 + "~2"
    assert len(skroty["2"]) <= 40
    assert skroty["1"] != skroty["2"]


def test_skrot_nazwa_krotsza_niz_40_rowna_sie_nazwie():
    """Nazwa krótsza niż limit -> kod = cała nazwa, bez modyfikacji."""
    nazwa = "Krótka Nazwa Sp. z o.o."
    assert len(nazwa) < 40
    k = HKontrahent(klucz="1", nazwa=nazwa)
    skroty = _unikalne_skroty([k])
    assert skroty["1"] == nazwa


# --------------------------------------------------------------------------- #
# sortowanie naturalne po numerze (_klucz_numeru)


def _mini_dok(numer: str, data: str = "2026-01-01") -> HDokument:
    """Dokument minimalny do testów samego sortowania — bez treści merytorycznej."""
    kontrahent = HKontrahent(klucz="K1", nazwa="Test")
    return HDokument(
        symbol="FVS", rodzaj_dok="sprzedaży", numer=numer,
        data_wystawienia=data, data_operacji=data, data_wplywu="",
        termin_platnosci="", kwota="0.00", mpp=False,
        waluta="", kurs="", wkwota="",
        ksef_number="", ksef_date="", earchive_id="",
        kontrahent_klucz="K1", kontrahent=kontrahent, rejestry=[],
    )


def test_build_file_sortowanie_naturalna_2_przed_10():
    """"FV/2/2026" przed "FV/10/2026" — leksykograficznie byłoby odwrotnie
    (bo "1" < "2" jako pierwszy znak), sortowanie naturalne porównuje segmenty
    "2" i "10" liczbowo."""
    text = build_hmffk_text([], [_mini_dok("FV/10/2026"), _mini_dok("FV/2/2026")])
    assert text.index("FK nazwa =FV/2/2026") < text.index("FK nazwa =FV/10/2026")


def test_build_file_sortowanie_mieszane_formaty_bez_typeerror():
    """Numery od różnych dostawców w jednym pliku, o zupełnie różnych formatach
    (z cyfrowym prefiksem, z prefiksem literowym, ze spacją) — sortowanie nie
    może rzucić TypeError (int vs str przy porównaniu) i musi dać deterministyczny,
    powtarzalny porządek niezależny od kolejności wejściowej."""
    numery = ["00019006/SK/N/EUR/2026/05", "A/1", "FV 7/26"]
    dokumenty = [_mini_dok(n) for n in numery]

    text_wprost = build_hmffk_text([], dokumenty)
    text_odwrocone = build_hmffk_text([], list(reversed(dokumenty)))

    assert text_wprost == text_odwrocone  # deterministyczne bez względu na wejście

    oczekiwana_kolejnosc = sorted(numery, key=_klucz_numeru)
    pozycje = [text_wprost.index(f"FK nazwa ={n}") for n in oczekiwana_kolejnosc]
    assert pozycje == sorted(pozycje)


def test_klucz_numeru_porownania_par():
    """_klucz_numeru: cyfry porównywane liczbowo (nie leksykograficznie), tekst
    bez rozróżniania wielkości liter, a znacznik (0=liczba/1=tekst) w każdej
    parze krotki gwarantuje, że porównanie różnych formatów numerów nigdy nie
    zestawia int z str."""
    # cyfry: porównanie liczbowe, nie po pierwszym znaku
    assert _klucz_numeru("2") < _klucz_numeru("10")
    assert _klucz_numeru("FV/2/2026") < _klucz_numeru("FV/10/2026")

    # tekst: bez rozróżniania wielkości liter
    assert _klucz_numeru("fv/1") == _klucz_numeru("FV/1")

    # różne formaty numerów -- porównanie zawsze rozstrzyga (antysymetria),
    # bez TypeError
    a = _klucz_numeru("A/1")
    b = _klucz_numeru("00019006/SK/N/EUR/2026/05")
    c = _klucz_numeru("FV 7/26")
    assert (a < b) != (b < a)
    assert (a < c) != (c < a)
    assert (b < c) != (c < b)

    # budowa krotki: pary (znacznik, wartość) -- 0 dla przebiegu cyfr (int),
    # 1 dla przebiegu tekstu (str.casefold)
    assert _klucz_numeru("1") == ((0, 1),)
    assert _klucz_numeru("A") == ((1, "a"),)
    assert _klucz_numeru("FV/2/2026") == ((1, "fv/"), (0, 2), (1, "/"), (0, 2026))
