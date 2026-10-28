"""Testy rejestru numerów kontrahentów (numeracja kart FK)."""

import csv
import hashlib
import io
from pathlib import Path

import pytest
from openpyxl import Workbook

import app.kontrahenci_nr as kontrahenci_nr_mod
from app.kontrahenci_nr import RejestrNumerow, SynchronizacjaOdrzucona, _digits

PROFIL = "5252445719"

NAGLOWKI = ["Pozycja", "Kod", "Nazwa", "NIP", "Adres", "Kod pocztowy", "Miejscowość", "Rejon"]

FIXTURES = Path(__file__).parent / "fixtures"


def _klucz_hash(nazwa: str, miejscowosc: str) -> str:
    """Ta sama formuła co app/hmffk.py::_kontrahent_z_podmiotu dla braku NIP."""
    digest = hashlib.sha1(f"{nazwa}|{miejscowosc}".encode("utf-8"))
    return "K_" + digest.hexdigest()[:12].upper()


def test_przydziel_max_plus_1_i_idempotencja(tmp_path):
    rej = RejestrNumerow(tmp_path / "nr.db")
    assert rej.przydziel(PROFIL, "A") == 1
    assert rej.przydziel(PROFIL, "B") == 2
    assert rej.przydziel(PROFIL, "C") == 3

    rej.usun(PROFIL, "B")
    assert rej.pozycja(PROFIL, "B") is None

    # nowy kontrahent dostaje max+1 (4), NIE zwolniony numer (2) — FK nie
    # zasypuje dziur po usuniętych kartach
    assert rej.przydziel(PROFIL, "D") == 4

    # idempotencja: ponowne przydzielenie istniejącego klucza zwraca tę samą pozycję
    assert rej.przydziel(PROFIL, "A") == 1
    assert rej.przydziel(PROFIL, "D") == 4
    rej.close()


def test_seed_z_xlsx_i_kontynuacja_numeracji(tmp_path):
    xlsx_path = tmp_path / "kartoteka.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(NAGLOWKI)
    # wiersz 2 (i=2): pełny NIP z prefiksem PL
    ws.append([1, "KOW", "Kowalski Sp. z o.o.", "PL5252445719", "Testowa 1", "00-001", "Warszawa", "1"])
    # wiersz 3 (i=3): brak NIP -> klucz z hasha
    ws.append([2, "NOW", "Nowak Jan", "", "Polna 2", "00-002", "Kraków", "1"])
    # wiersz 4 (i=4): brak pozycji -> pominięty
    ws.append(["", "BRK", "Brak pozycji", "", "Dluga 3", "00-003", "Lodz", "1"])
    # wiersz 5 (i=5): kolizja pozycji (1 zajęte) -> pominięty
    ws.append([1, "KOL", "Kolizja pozycji", "9999999999", "Krotka 4", "00-004", "Gdansk", "1"])
    # wiersz 6 (i=6): kolizja klucza (ten sam NIP co wiersz 2) -> pominięty
    ws.append([3, "DUP", "Kowalski Sp. z o.o.", "PL5252445719", "Testowa 1", "00-001", "Warszawa", "1"])
    wb.save(xlsx_path)

    rej = RejestrNumerow(tmp_path / "nr.db")
    dodane, ostrzezenia = rej.importuj_kartoteke(PROFIL, xlsx_path)

    assert dodane == 2
    # 3 pominięcia (brak pozycji, kolizja pozycji, kolizja klucza) + 1 ostrzeżenie
    # o braku NIP dla wiersza "Nowak Jan" (dodanego mimo to, z kluczem przybliżonym)
    assert len(ostrzezenia) == 4
    assert any("brak NIP" in o and "Nowak Jan" in o for o in ostrzezenia)

    assert rej.pozycja(PROFIL, "5252445719") == 1
    oczekiwany_klucz2 = _klucz_hash("Nowak Jan", "Kraków")
    assert rej.pozycja(PROFIL, oczekiwany_klucz2) == 2

    wpisy = {w.klucz: w for w in rej.lista(PROFIL)}
    assert wpisy["5252445719"].status == "seed"
    assert wpisy["5252445719"].nazwa == "Kowalski Sp. z o.o."
    assert wpisy[oczekiwany_klucz2].status == "seed"

    # kontynuacja numeracji: pozycja 3 wolna (wiersz 6 odrzucony jako kolizja klucza)
    assert rej.przydziel(PROFIL, "NOWY") == 3
    rej.close()


def test_seed_z_xlsx_nip_jako_liczba_bez_falszywego_zera(tmp_path):
    """Regresja: NIP wpisany w Excelu jako liczba (bez formatu tekstowego) wraca
    z openpyxl jako float 9100010014.0 — naiwne str(val) dałoby "...90.0", a
    _digits zebrałoby cyfry WRAZ z fałszywym zerem z ".0" (11 cyfr zamiast 10),
    przez co kontrahent z faktury (klucz z XML, 10 cyfr) nie dopasowałby się do
    seedu i dostałby cichy duplikat karty. Klucz i nip po imporcie mają być
    "9100010014" (10 cyfr), a przydziel() dla tego klucza ma trafić w
    zaimportowaną pozycję 5, bez nowego przydziału."""
    xlsx_path = tmp_path / "kartoteka_liczba.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(NAGLOWKI)
    ws.append(
        [5, "MERIDOL", "MERIDOL JAN KOWALSKI", 9100010014, "Testowa 4", "40-158", "Katowice", "śląskie"]
    )
    wb.save(xlsx_path)

    rej = RejestrNumerow(tmp_path / "nr.db")
    dodane, ostrzezenia = rej.importuj_kartoteke(PROFIL, xlsx_path)

    assert dodane == 1
    assert ostrzezenia == []

    wpisy = {w.klucz: w for w in rej.lista(PROFIL)}
    assert "9100010014" in wpisy
    assert wpisy["9100010014"].nip == "9100010014"
    assert wpisy["9100010014"].pozycja == 5

    # przydziel dla klucza z faktury (10 cyfr) trafia w zaimportowaną kartę
    assert rej.przydziel(PROFIL, "9100010014") == 5
    rej.close()


def test_digits_odcina_sufiks_floatowy_z_excela():
    assert _digits("9100010014.0") == "9100010014"


def test_digits_usuwa_prefiks_pl():
    assert _digits("PL9100010037") == "9100010037"


def test_aliasy_zwolnij_numer_true_zwalnia_pozycje(tmp_path):
    rej = RejestrNumerow(tmp_path / "nr.db")
    assert rej.przydziel(PROFIL, "KANON") == 1
    assert rej.przydziel(PROFIL, "DUP1") == 2

    rej.dodaj_alias(PROFIL, "DUP1", "KANON", zwolnij_numer=True)

    assert rej.kanoniczny(PROFIL, "DUP1") == "KANON"
    assert rej.pozycja(PROFIL, "DUP1") is None  # karta usunięta w FK -> wpis skasowany
    # pozycja 2 wraca do puli
    assert rej.przydziel(PROFIL, "NOWY") == 2
    rej.close()


def test_aliasy_zwolnij_numer_false_zostawia_status_duplikat(tmp_path):
    rej = RejestrNumerow(tmp_path / "nr.db")
    assert rej.przydziel(PROFIL, "KANON") == 1
    assert rej.przydziel(PROFIL, "DUP2") == 2

    rej.dodaj_alias(PROFIL, "DUP2", "KANON", zwolnij_numer=False)

    assert rej.kanoniczny(PROFIL, "DUP2") == "KANON"
    assert rej.pozycja(PROFIL, "DUP2") == 2  # numer nadal zajęty
    wpis = next(w for w in rej.lista(PROFIL) if w.klucz == "DUP2")
    assert wpis.status == "duplikat"

    # pozycja 2 NIE wraca do puli -> kolejny nowy kontrahent dostaje 3
    assert rej.przydziel(PROFIL, "NOWY2") == 3

    rej.usun_alias(PROFIL, "DUP2")
    assert rej.aliasy(PROFIL) == {}
    assert rej.kanoniczny(PROFIL, "DUP2") == "DUP2"
    rej.close()


def test_aliasy_lancuch_splaszczany_do_punktu_stalego(tmp_path):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.przydziel(PROFIL, "B")
    rej.przydziel(PROFIL, "A")
    rej.przydziel(PROFIL, "C")

    rej.dodaj_alias(PROFIL, "A", "B", zwolnij_numer=True)
    rej.dodaj_alias(PROFIL, "C", "A", zwolnij_numer=True)

    # w bazie C wskazuje WPROST na B (nie na A) — bez łańcucha
    assert rej.aliasy(PROFIL)["C"] == "B"
    assert rej.kanoniczny(PROFIL, "C") == "B"
    rej.close()


def test_aliasy_dodanie_do_juz_zaaliasowanego_klucza_przepina_istniejace(tmp_path):
    """Kolejność odwrotna niż wyżej: C->A istnieje jako pierwsze, dopiero potem
    A staje się aliasem B — istniejący wpis C musi zostać przepięty na B."""
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.przydziel(PROFIL, "B")
    rej.przydziel(PROFIL, "A")
    rej.przydziel(PROFIL, "C")

    rej.dodaj_alias(PROFIL, "C", "A", zwolnij_numer=True)
    rej.dodaj_alias(PROFIL, "A", "B", zwolnij_numer=True)

    assert rej.aliasy(PROFIL)["C"] == "B"
    assert rej.kanoniczny(PROFIL, "C") == "B"
    rej.close()


def test_aliasy_proba_cyklu_zglasza_blad(tmp_path):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.przydziel(PROFIL, "A")
    rej.przydziel(PROFIL, "B")
    rej.dodaj_alias(PROFIL, "A", "B", zwolnij_numer=True)

    with pytest.raises(ValueError):
        rej.dodaj_alias(PROFIL, "B", "A", zwolnij_numer=True)

    # bezpośredni cykl 1-elementowy (samo na siebie) też jest odrzucany
    with pytest.raises(ValueError):
        rej.dodaj_alias(PROFIL, "X", "X", zwolnij_numer=True)
    rej.close()


def test_usun_celu_scalenia_kaskadowo_usuwa_aliasy(tmp_path):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.przydziel(PROFIL, "KANON")
    rej.przydziel(PROFIL, "DUP")
    rej.dodaj_alias(PROFIL, "DUP", "KANON", zwolnij_numer=False)

    liczba = rej.usun(PROFIL, "KANON")

    assert liczba == 1
    assert rej.aliasy(PROFIL) == {}
    assert rej.kanoniczny(PROFIL, "DUP") == "DUP"
    rej.close()


def test_usun_bez_wiszacych_aliasow_zwraca_zero(tmp_path):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.przydziel(PROFIL, "A")
    assert rej.usun(PROFIL, "A") == 0
    rej.close()


def test_podobni_wielkosc_liter_ogonki_spacje(tmp_path):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "K1", 1, nazwa="Górski Kacper")
    rej.ustaw(PROFIL, "K4", 4, nazwa="Zupełnie inna, niepodobna nazwa firmy")

    wyniki = rej.podobni(PROFIL, "  gorski    KACPER  ")
    assert len(wyniki) == 1
    wpis, ratio = wyniki[0]
    assert wpis.klucz == "K1"
    assert ratio == 1.0
    rej.close()


def test_podobni_permutacja_tokenow(tmp_path):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "K2", 2, nazwa="SZERTEST NOWAK PIOTR")

    wyniki = rej.podobni(PROFIL, "SZERTEST PIOTR NOWAK")
    klucze = [w.klucz for w, _ in wyniki]
    assert "K2" in klucze
    rej.close()


def test_podobni_literowka_jednoznakowa_i_brak_dopasowania(tmp_path):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "K3", 3, nazwa="Kowalski Sp. z o.o.")
    rej.ustaw(PROFIL, "K4", 4, nazwa="Zupełnie inna, niepodobna nazwa firmy")

    wyniki = rej.podobni(PROFIL, "Kowalska Sp. z o.o.")
    trafienia = {w.klucz: ratio for w, ratio in wyniki}
    assert "K3" in trafienia
    assert trafienia["K3"] >= 0.87

    # zupełnie inna nazwa nie trafia w żaden wpis
    wyniki_brak = rej.podobni(PROFIL, "Instalacje Elektryczne Nowak")
    assert wyniki_brak == []
    rej.close()


def test_podobni_rdzen_bez_falszywych_trafien_z_forma_prawna(tmp_path):
    """Krótkie, różne rdzenie nie mogą trafiać mimo wspólnej, długiej formy prawnej
    lub generycznego prefiksu — SequenceMatcher po CAŁEJ nazwie dawał tu fałszywe
    trafienia (ABC/ABD: 0.93, PPHU Max/Mix: 0.875)."""
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "K1", 1, nazwa="ABC Sp. z o.o.")
    rej.ustaw(PROFIL, "K2", 2, nazwa="PPHU Max")

    assert rej.podobni(PROFIL, "ABD Sp. z o.o.") == []
    assert rej.podobni(PROFIL, "PPHU Mix") == []
    rej.close()


def test_podobni_rdzen_literowka_w_dlugiej_nazwie_nadal_trafia(tmp_path):
    """Literówka w rdzeniu wystarczająco długim (>=6 znaków) nadal ma być
    trafieniem — próg fuzzy działa na rdzeniu, nie tylko na całej nazwie."""
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "K1", 1, nazwa="KOSMETEST ALINA WÓJCIK")

    wyniki = rej.podobni(PROFIL, "KOSMETEST ALINA WÓJCIM")
    klucze = [w.klucz for w, _ in wyniki]
    assert "K1" in klucze
    rej.close()


def test_ustaw_konflikt_pozycji(tmp_path):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.przydziel(PROFIL, "A")  # pozycja 1
    rej.ustaw(PROFIL, "B", 2, nazwa="Firma B")
    assert rej.pozycja(PROFIL, "B") == 2

    with pytest.raises(ValueError):
        rej.ustaw(PROFIL, "C", 2, nazwa="Firma C")

    # zmiana pozycji tego samego klucza nie jest konfliktem
    rej.ustaw(PROFIL, "B", 5)
    assert rej.pozycja(PROFIL, "B") == 5
    rej.close()


def test_wyczysc_profil_usuwa_wpisy_i_aliasy_tylko_danego_profilu(tmp_path):
    """Procedura naprawcza po eksporcie bez seedu: kasuje WSZYSTKIE wpisy numery
    i aliasy jednego profilu, zwraca liczbę usuniętych wpisów numery, i nie rusza
    innego profilu."""
    rej = RejestrNumerow(tmp_path / "nr.db")
    INNY_PROFIL = "9999999999"

    rej.przydziel(PROFIL, "A")
    rej.przydziel(PROFIL, "B")
    rej.przydziel(PROFIL, "C")
    rej.dodaj_alias(PROFIL, "B", "A", zwolnij_numer=False)
    rej.przydziel(INNY_PROFIL, "X")  # inny profil — ma zostać nietknięty

    usuniete = rej.wyczysc_profil(PROFIL)

    assert usuniete == 3  # A, B (status 'duplikat', ale nadal wpis w numery), C
    assert rej.lista(PROFIL) == []
    assert rej.aliasy(PROFIL) == {}
    assert rej.pozycja(INNY_PROFIL, "X") == 1  # inny profil bez zmian
    rej.close()


def test_wyczysc_profil_pustego_profilu_zwraca_zero(tmp_path):
    rej = RejestrNumerow(tmp_path / "nr.db")
    assert rej.wyczysc_profil(PROFIL) == 0
    rej.close()


def test_zmien_dane_rename_klucza_po_zmianie_nip_przepina_aliasy(tmp_path):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "STARY", 1, nazwa="Firma Testowa", miejscowosc="Warszawa")
    rej.przydziel(PROFIL, "INNY")  # pozycja 2
    rej.dodaj_alias(PROFIL, "INNY", "STARY", zwolnij_numer=False)

    nowy = rej.zmien_dane(
        PROFIL,
        "STARY",
        pozycja=1,
        nip="5252445719",
        nazwa="Firma Testowa",
        miejscowosc="Warszawa",
        status="manual",
    )

    assert nowy == "5252445719"
    assert rej.pozycja(PROFIL, "STARY") is None  # stary klucz zniknął

    wpisy = {w.klucz: w for w in rej.lista(PROFIL)}
    assert nowy in wpisy
    wpis = wpisy[nowy]
    assert wpis.pozycja == 1
    assert wpis.nazwa == "Firma Testowa"
    assert wpis.nip == "5252445719"
    assert wpis.miejscowosc == "Warszawa"
    assert wpis.status == "manual"

    # alias wskazujący na stary klucz teraz wskazuje na nowy
    assert rej.aliasy(PROFIL)["INNY"] == nowy
    assert rej.kanoniczny(PROFIL, "INNY") == nowy
    rej.close()


@pytest.mark.parametrize(
    "status, nowy_alias",
    [("duplikat", True), ("manual", False)],
)
def test_zmien_dane_rename_wiersza_bedacego_aliasem_zachowuje_stary_alias(
    tmp_path, status, nowy_alias
):
    """Stary klucz był ALIASEM (wiersz-duplikat scalony z KANON bez zwolnienia
    numeru). Dawniej alias był PRZENOSZONY na nowy klucz, więc faktury ze
    starym kluczem (liczonym z KSeF) traciły kartę KANON i dostawały nowy
    numer. Teraz stary alias zostaje; nowy klucz wskazuje KANON tylko wtedy,
    gdy wiersz nadal jest duplikatem (status duplikat)."""
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "KANON", 1, nazwa="Kanoniczna Firma")
    rej.ustaw(PROFIL, "STARY", 2, nazwa="Duplikat Firma", miejscowosc="Poznań")
    rej.dodaj_alias(PROFIL, "STARY", "KANON", zwolnij_numer=False)

    nowy = rej.zmien_dane(
        PROFIL,
        "STARY",
        pozycja=2,
        nip="1111111111",
        nazwa="Duplikat Firma",
        miejscowosc="Poznań",
        status=status,
    )

    oczekiwane = {"STARY": "KANON"}
    if nowy_alias:
        oczekiwane[nowy] = "KANON"
    assert rej.aliasy(PROFIL) == oczekiwane
    assert rej.kanoniczny(PROFIL, "STARY") == "KANON"
    assert rej.kanoniczny(PROFIL, nowy) == ("KANON" if nowy_alias else nowy)
    rej.close()


def test_zmien_dane_zmiana_klucza_dopisuje_alias_stary_na_nowy(tmp_path):
    """W1: klucz faktur z KSeF to STARY klucz — po zmianie nazwy (bez NIP) albo
    NIP-u alias stary -> nowy prowadzi kolejne faktury na tę samą kartę."""
    stary_klucz = _klucz_hash("Kowalski Jan", "Kraków")
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, stary_klucz, 7, nazwa="Kowalski Jan", miejscowosc="Kraków")

    nowy = rej.zmien_dane(
        PROFIL, stary_klucz, pozycja=7, nip="", nazwa="Kowalski Jan Usługi",
        miejscowosc="Kraków", status="manual",
    )

    assert rej.aliasy(PROFIL) == {stary_klucz: nowy}
    assert rej.pozycja(PROFIL, rej.kanoniczny(PROFIL, stary_klucz)) == 7
    # i z powrotem — bez cyklu i bez martwego wiersza
    rej.zmien_dane(
        PROFIL, nowy, pozycja=7, nip="", nazwa="Kowalski Jan", miejscowosc="Kraków",
        status="manual",
    )
    assert rej.aliasy(PROFIL) == {nowy: stary_klucz}
    assert rej.kanoniczny(PROFIL, stary_klucz) == stary_klucz
    rej.close()


def test_zmien_dane_zmiana_nazwy_bez_nip_zmienia_klucz(tmp_path):
    stary_klucz = _klucz_hash("Kowalski Jan", "Kraków")
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, stary_klucz, 1, nazwa="Kowalski Jan", miejscowosc="Kraków")

    oczekiwany_nowy = _klucz_hash("Kowalska Anna", "Kraków")
    nowy = rej.zmien_dane(
        PROFIL,
        stary_klucz,
        pozycja=1,
        nip="",
        nazwa="Kowalska Anna",
        miejscowosc="Kraków",
        status="manual",
    )

    assert nowy == oczekiwany_nowy
    assert nowy != stary_klucz
    assert rej.pozycja(PROFIL, stary_klucz) is None
    wpis = next(w for w in rej.lista(PROFIL) if w.klucz == nowy)
    assert wpis.nazwa == "Kowalska Anna"
    rej.close()


def test_zmien_dane_konflikt_istniejacego_klucza_nie_zmienia_nic(tmp_path):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "5252445719", 1, nazwa="Firma A", nip="5252445719")
    rej.ustaw(PROFIL, "STARY", 2, nazwa="Firma B", miejscowosc="Kraków")

    with pytest.raises(ValueError):
        rej.zmien_dane(
            PROFIL,
            "STARY",
            pozycja=2,
            nip="5252445719",
            nazwa="Firma B",
            miejscowosc="Kraków",
            status="manual",
        )

    # nic się nie zmieniło
    assert rej.pozycja(PROFIL, "STARY") == 2
    assert rej.pozycja(PROFIL, "5252445719") == 1
    wpis = next(w for w in rej.lista(PROFIL) if w.klucz == "STARY")
    assert wpis.nazwa == "Firma B"
    rej.close()


def test_zmien_dane_konflikt_pozycji(tmp_path):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "5252445719", 1, nazwa="Firma A", nip="5252445719")
    rej.ustaw(PROFIL, "9999999999", 2, nazwa="Firma B", nip="9999999999")

    with pytest.raises(ValueError):
        rej.zmien_dane(
            PROFIL,
            "9999999999",
            pozycja=1,
            nip="9999999999",
            nazwa="Firma B",
            miejscowosc="",
            status="manual",
        )

    assert rej.pozycja(PROFIL, "9999999999") == 2
    rej.close()


def test_zmien_dane_pusty_nip_i_nazwa_blad(tmp_path):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "A", 1, nazwa="Firma A")

    with pytest.raises(ValueError):
        rej.zmien_dane(
            PROFIL, "A", pozycja=1, nip="", nazwa="", miejscowosc="Warszawa", status="manual"
        )
    rej.close()


def test_utworz_wpis_pozycja_none_bierze_max_plus_1(tmp_path):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.przydziel(PROFIL, "A")  # pozycja 1
    rej.przydziel(PROFIL, "B")  # pozycja 2
    rej.usun(PROFIL, "A")  # zwalnia pozycję 1

    klucz, pozycja = rej.utworz_wpis(PROFIL, nazwa="Nowa Firma", miejscowosc="Gdańsk")

    # max+1 (3), NIE zwolniona pozycja 1 — jak przydziel()
    assert pozycja == 3
    assert klucz == _klucz_hash("Nowa Firma", "Gdańsk")
    assert rej.pozycja(PROFIL, klucz) == 3
    rej.close()


def test_utworz_wpis_pusty_nip_i_nazwa_blad(tmp_path):
    rej = RejestrNumerow(tmp_path / "nr.db")

    with pytest.raises(ValueError):
        rej.utworz_wpis(PROFIL, nip="", nazwa="", miejscowosc="Warszawa")
    rej.close()


def test_eksport_csv(tmp_path):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "A", 1, nazwa="Firma A", nip="123", miejscowosc="Warszawa")

    out = tmp_path / "eksport.csv"
    rej.eksportuj_csv(PROFIL, out)

    tresc = out.read_text(encoding="utf-8-sig")
    linie = tresc.splitlines()
    assert linie[0] == "Pozycja;Klucz;NIP;Nazwa;Miejscowosc;Status"
    assert linie[1] == "1;A;123;Firma A;Warszawa;manual"
    rej.close()


# ---------------------------------------------------------------------- #
# import z różnych kształtów plików CSV/xlsx (odporna detekcja nagłówka) —
# patrz app/kontrahenci_nr.py::_naglowki_z_wiersza / _czy_wiersz_naglowka /
# _mapuj_wiersze_danych, użyte przez _wczytaj_xlsx i _wczytaj_csv


def _zapisz_csv_google(sciezka: Path, wiersze: list[list[str]]) -> None:
    """Serializuje wiersze do CSV (przecinek, cudzysłowy tam gdzie trzeba —
    jak prawdziwy eksport) i zapisuje z BOM UTF-8, jak realny plik z Arkuszy
    Google."""
    buf = io.StringIO()
    csv.writer(buf, delimiter=",").writerows(wiersze)
    sciezka.write_bytes(("﻿" + buf.getvalue()).encode("utf-8"))


def test_import_csv_replika_pliku_arkuszy_google(tmp_path):
    """Replika DOKŁADNIE kształtu realnego eksportu z Arkuszy Google: pierwszy
    wiersz śmieciowy (same puste pola), nagłówek dopiero w drugim, po Rejonie
    dwie nadmiarowe kolumny bez nagłówka, nazwa w cudzysłowie z przecinkiem
    w środku, NIP z prefiksem PL, i wiersz bez NIP."""
    wiersze = [
        ["", "", "", "", "", "", "", "", "", ""],  # śmieciowy pierwszy wiersz
        ["Pozycja", "Kod", "Nazwa", "NIP", "Adres", "Kod pocztowy", "Miejscowość", "Rejon", "", ""],
        ["1", "SYM", "Symfonia sp. z o.o.", "5262831503", "Al. Jerozolimskie 132", "02-305", "Warszawa", "", "1", "1"],
        [
            "2", "GED",
            "TESTDENT STOMATOLOGIA S.C. MAREK LEWANDOWSKI, ALEKSANDRA KAMIŃSKA",
            "9100010178", "Testowa 12", "41-940", "Piekary Śląskie", "śląskie", "2", "2",
        ],
        ["3", "ECO", "ECO-TESTCLEAN Karolina Dąbrowska", "PL9100010037", "", "78-100", "", "", "3", "3"],
        ["4", "MED", "MEDITESTCO CO.,LTD CN/TESTCITY CITY,", "", "", "", "", "", "4", "4"],
    ]
    sciezka = tmp_path / "kartoteka_google.csv"
    _zapisz_csv_google(sciezka, wiersze)

    rej = RejestrNumerow(tmp_path / "nr.db")
    dodane, ostrzezenia = rej.importuj_kartoteke(PROFIL, sciezka)

    assert dodane == 4
    assert len(ostrzezenia) == 1
    assert "brak NIP" in ostrzezenia[0]

    assert rej.pozycja(PROFIL, "5262831503") == 1
    assert rej.pozycja(PROFIL, "9100010178") == 2
    assert rej.pozycja(PROFIL, "9100010037") == 3  # PL-prefiks zdjęty

    klucz_meditestco = _klucz_hash("MEDITESTCO CO.,LTD CN/TESTCITY CITY,", "")
    assert rej.pozycja(PROFIL, klucz_meditestco) == 4

    wpisy = {w.klucz: w for w in rej.lista(PROFIL)}
    assert wpisy["9100010178"].nazwa == (
        "TESTDENT STOMATOLOGIA S.C. MAREK LEWANDOWSKI, ALEKSANDRA KAMIŃSKA"
    )
    rej.close()


def test_import_xlsx_pusty_pierwszy_wiersz_naglowek_w_drugim(tmp_path):
    xlsx_path = tmp_path / "kartoteka_pusty_wiersz.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["", "", "", "", "", "", "", ""])  # pusty (śmieciowy) pierwszy wiersz
    ws.append(NAGLOWKI)
    ws.append([1, "ABC", "Firma Testowa", "5252445719", "Testowa 1", "00-001", "Warszawa", "1"])
    wb.save(xlsx_path)

    rej = RejestrNumerow(tmp_path / "nr.db")
    dodane, ostrzezenia = rej.importuj_kartoteke(PROFIL, xlsx_path)

    assert dodane == 1
    assert ostrzezenia == []
    assert rej.pozycja(PROFIL, "5252445719") == 1
    rej.close()


def test_import_csv_srednik_bez_smieciowego_wiersza_regresja(tmp_path):
    """Regresja: zwykły plik CSV ze średnikiem jako separatorem, nagłówek od
    razu w pierwszym wierszu (bez śmieciowego wiersza) — musi działać tak jak
    dotychczas."""
    csv_path = tmp_path / "kartoteka_srednik.csv"
    tresc = (
        "Pozycja;Kod;Nazwa;NIP;Adres;Kod pocztowy;Miejscowość;Rejon\n"
        "1;ABC;Firma Testowa;5252445719;Testowa 1;00-001;Warszawa;1\n"
        "2;DEF;Inna Firma;9999999999;Polna 2;00-002;Kraków;1\n"
    )
    csv_path.write_text(tresc, encoding="utf-8-sig")

    rej = RejestrNumerow(tmp_path / "nr.db")
    dodane, ostrzezenia = rej.importuj_kartoteke(PROFIL, csv_path)

    assert dodane == 2
    assert ostrzezenia == []
    assert rej.pozycja(PROFIL, "5252445719") == 1
    assert rej.pozycja(PROFIL, "9999999999") == 2
    rej.close()


def test_import_csv_bez_naglowka_zglasza_blad(tmp_path):
    csv_path = tmp_path / "bez_naglowka.csv"
    tresc = "\n".join(f"wiersz,{i},smieci,{i * 2}" for i in range(12))
    csv_path.write_text(tresc, encoding="utf-8-sig")

    rej = RejestrNumerow(tmp_path / "nr.db")
    with pytest.raises(ValueError):
        rej.importuj_kartoteke(PROFIL, csv_path)
    rej.close()


def test_import_xlsx_bez_naglowka_zglasza_blad(tmp_path):
    xlsx_path = tmp_path / "bez_naglowka.xlsx"
    wb = Workbook()
    ws = wb.active
    for i in range(12):
        ws.append(["Foo", "Bar", i])
    wb.save(xlsx_path)

    rej = RejestrNumerow(tmp_path / "nr.db")
    with pytest.raises(ValueError):
        rej.importuj_kartoteke(PROFIL, xlsx_path)
    rej.close()


def test_import_prawdziwy_plik_eksportu_z_arkuszy_google(tmp_path):
    """Test integracyjny na PRAWDZIWYM pliku wyeksportowanym z Arkuszy Google
    (skopiowanym do tests/fixtures, żeby test nie zależał od Downloads)."""
    sciezka = FIXTURES / "kartoteka_google_sheets.csv"
    rej = RejestrNumerow(tmp_path / "nr.db")
    dodane, ostrzezenia = rej.importuj_kartoteke(PROFIL, sciezka)

    assert dodane == 33
    assert len(ostrzezenia) == 2
    assert all("brak NIP" in o for o in ostrzezenia)

    wpisy = rej.lista(PROFIL)
    assert sorted(w.pozycja for w in wpisy) == list(range(1, 34))

    assert rej.pozycja(PROFIL, "9100010014") == 5  # MERIDOL JAN KOWALSKI
    assert rej.pozycja(PROFIL, "9100010037") == 16  # ECO-TESTCLEAN, NIP z prefiksem PL

    wpisy_po_pozycji = {w.pozycja: w for w in wpisy}
    assert wpisy_po_pozycji[19].klucz.startswith("K_")  # MEDITESTCO — brak NIP
    assert wpisy_po_pozycji[25].klucz.startswith("K_")  # PURTEST — brak NIP

    rej.close()


# ---------------------------------------------------------------------- #
# synchronizuj_z_kartoteka — kartoteka FK jako źródło prawdy dla rejestru
# rozjechanego z ręcznymi zmianami zrobionymi bezpośrednio w Symfonii


def _kartoteka_xlsx(sciezka: Path, wiersze: list[list]) -> None:
    """Zapisuje xlsx w kształcie eksportu kartoteki FK (kolumny jak NAGLOWKI)."""
    wb = Workbook()
    ws = wb.active
    ws.append(NAGLOWKI)
    for w in wiersze:
        ws.append(w)
    wb.save(sciezka)


def test_sync_dodaje_nowe_klucze_z_pustego_rejestru(tmp_path):
    sciezka = tmp_path / "kartoteka.xlsx"
    _kartoteka_xlsx(sciezka, [
        [1, "A", "Firma A", "5252445719", "Testowa 1", "00-001", "Warszawa", "1"],
        [2, "B", "Firma B", "9999999999", "Polna 2", "00-002", "Kraków", "1"],
    ])
    rej = RejestrNumerow(tmp_path / "nr.db")

    raport = rej.synchronizuj_z_kartoteka(PROFIL, sciezka)

    assert raport.dodane == 2
    assert raport.zaktualizowane == 0
    assert raport.rozjazdy == []
    assert raport.brak_w_fk == []
    assert rej.pozycja(PROFIL, "5252445719") == 1
    assert rej.pozycja(PROFIL, "9999999999") == 2
    wpisy = {w.klucz: w for w in rej.lista(PROFIL)}
    assert wpisy["5252445719"].status == "seed"
    rej.close()


def test_sync_ta_sama_pozycja_uzupelnia_tylko_puste_pola(tmp_path):
    rej = RejestrNumerow(tmp_path / "nr.db")
    # wpis istnieje już na właściwej pozycji, ale bez nazwy/miejscowości —
    # jakby założony ręcznie w rejestrze z samym NIP-em
    rej.ustaw(PROFIL, "5252445719", 1, nip="5252445719", nazwa="", miejscowosc="", status="seed")

    sciezka = tmp_path / "kartoteka.xlsx"
    _kartoteka_xlsx(sciezka, [
        [1, "A", "Firma A Sp. z o.o.", "5252445719", "Testowa 1", "00-001", "Warszawa", "1"],
    ])

    raport = rej.synchronizuj_z_kartoteka(PROFIL, sciezka)

    assert raport.dodane == 0
    assert raport.zaktualizowane == 1
    assert raport.rozjazdy == []
    wpis = next(w for w in rej.lista(PROFIL) if w.klucz == "5252445719")
    assert wpis.nazwa == "Firma A Sp. z o.o."
    assert wpis.miejscowosc == "Warszawa"
    assert wpis.pozycja == 1  # bez zmian
    rej.close()


def test_sync_ta_sama_pozycja_nazwa_z_nip_z_fk_reszta_tylko_puste(tmp_path):
    """Nazwa kontrahenta z NIP-em idzie z FK (eksport wysyła ją do Symfonii,
    musi być równa nazwie karty); pozostałe pola — tylko uzupełniane. Wpis
    poprawiony ręcznie w tabeli ma status manual i nie jest ruszany (osobny test)."""
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "5252445719", 1, nip="5252445719", nazwa="Nazwa Z Pierwszej Faktury", status="seed")

    sciezka = tmp_path / "kartoteka.xlsx"
    _kartoteka_xlsx(sciezka, [
        [1, "A", "Nazwa Z Kartoteki FK", "5252445719", "Testowa 1", "00-001", "Warszawa", "1"],
    ])

    raport = rej.synchronizuj_z_kartoteka(PROFIL, sciezka)

    # nazwa z FK; miejscowość była pusta -> uzupełniona
    assert raport.zaktualizowane == 1
    wpis = next(w for w in rej.lista(PROFIL) if w.klucz == "5252445719")
    assert wpis.nazwa == "Nazwa Z Kartoteki FK"
    assert wpis.miejscowosc == "Warszawa"
    rej.close()


def test_sync_rozjazd_nadpisuje_pozycja_wartoscia_z_fk(tmp_path):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "5252445719", 5, nip="5252445719", nazwa="Firma A", status="seed")

    sciezka = tmp_path / "kartoteka.xlsx"
    _kartoteka_xlsx(sciezka, [
        [8, "A", "Firma A", "5252445719", "Testowa 1", "00-001", "Warszawa", "1"],
    ])

    raport = rej.synchronizuj_z_kartoteka(PROFIL, sciezka)

    assert raport.rozjazdy == [("5252445719", 5, 8)]
    assert raport.dodane == 0
    assert rej.pozycja(PROFIL, "5252445719") == 8
    rej.close()


def test_sync_zamiana_miejscami_bez_integrity_error(tmp_path):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "1111111111", 1, nip="1111111111", nazwa="Firma A", status="seed")
    rej.ustaw(PROFIL, "2222222222", 2, nip="2222222222", nazwa="Firma B", status="seed")

    sciezka = tmp_path / "kartoteka.xlsx"
    _kartoteka_xlsx(sciezka, [
        # zamienione miejscami względem rejestru: A ma być na 2, B na 1
        [2, "A", "Firma A", "1111111111", "Testowa 1", "00-001", "Warszawa", "1"],
        [1, "B", "Firma B", "2222222222", "Polna 2", "00-002", "Kraków", "1"],
    ])

    raport = rej.synchronizuj_z_kartoteka(PROFIL, sciezka)  # nie może rzucić IntegrityError

    assert sorted(raport.rozjazdy) == [("1111111111", 1, 2), ("2222222222", 2, 1)]
    assert rej.pozycja(PROFIL, "1111111111") == 2
    assert rej.pozycja(PROFIL, "2222222222") == 1
    rej.close()


def test_sync_brak_w_fk_nie_usuwa_wpisu(tmp_path):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "1111111111", 1, nip="1111111111", nazwa="Firma A")
    rej.ustaw(PROFIL, "ZDUPLIKOWANY", 2, nazwa="Karta-duplikat scalona aliasem")

    sciezka = tmp_path / "kartoteka.xlsx"
    _kartoteka_xlsx(sciezka, [
        [1, "A", "Firma A", "1111111111", "Testowa 1", "00-001", "Warszawa", "1"],
    ])

    raport = rej.synchronizuj_z_kartoteka(PROFIL, sciezka)

    assert raport.brak_w_fk == [("ZDUPLIKOWANY", 2)]
    # wpis nietknięty — nie usunięty, na tej samej pozycji
    assert rej.pozycja(PROFIL, "ZDUPLIKOWANY") == 2
    rej.close()


def test_sync_ewakuuje_konflikt_z_wpisem_nieobecnym_w_fk(tmp_path):
    """Pozycja potrzebna kluczowi z kartoteki jest zajęta w rejestrze przez wpis
    nieobecny w kartotece (brak_w_fk) — sync przesuwa go (nie usuwa), robiąc
    miejsce, i zgłasza ostrzeżenie."""
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "BLOKUJE", 3, nazwa="Blokuje pozycję 3", status="seed")

    sciezka = tmp_path / "kartoteka.xlsx"
    _kartoteka_xlsx(sciezka, [
        [3, "A", "Firma A", "1111111111", "Testowa 1", "00-001", "Warszawa", "1"],
    ])

    raport = rej.synchronizuj_z_kartoteka(PROFIL, sciezka)

    assert raport.dodane == 1
    assert rej.pozycja(PROFIL, "1111111111") == 3  # dostał pozycję z FK
    nowa_pozycja_blokujacego = rej.pozycja(PROFIL, "BLOKUJE")
    assert nowa_pozycja_blokujacego is not None
    assert nowa_pozycja_blokujacego != 3  # przesunięty, nie usunięty
    assert raport.brak_w_fk == [("BLOKUJE", nowa_pozycja_blokujacego)]
    assert any("przesunięte" in o for o in raport.ostrzezenia)
    rej.close()


def test_sync_idempotencja(tmp_path):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "1111111111", 5, nip="1111111111", nazwa="Firma A", status="seed")

    sciezka = tmp_path / "kartoteka.xlsx"
    _kartoteka_xlsx(sciezka, [
        [8, "A", "Firma A", "1111111111", "Testowa 1", "00-001", "Warszawa", "1"],
        [9, "B", "Firma B", "2222222222", "Polna 2", "00-002", "Kraków", "1"],
    ])

    pierwszy = rej.synchronizuj_z_kartoteka(PROFIL, sciezka)
    assert pierwszy.dodane == 1
    assert pierwszy.rozjazdy == [("1111111111", 5, 8)]

    stan_po_pierwszym = {w.klucz: (w.pozycja, w.nazwa) for w in rej.lista(PROFIL)}

    drugi = rej.synchronizuj_z_kartoteka(PROFIL, sciezka)

    assert drugi.dodane == 0
    assert drugi.zaktualizowane == 0
    assert drugi.rozjazdy == []
    assert drugi.brak_w_fk == []
    stan_po_drugim = {w.klucz: (w.pozycja, w.nazwa) for w in rej.lista(PROFIL)}
    assert stan_po_pierwszym == stan_po_drugim
    rej.close()


# ---------------------------------------------------------------------- #
# wpisy "manual" (numer ustawiony ręcznie przez księgową w oknie Kontrahenci…
# po sprawdzeniu w Symfonii) są nietykalne dla synchronizacji — rejestr jest
# źródłem prawdy o numerach kart; różnica trafia do raportu osobną kategorią


def test_sync_nie_nadpisuje_pozycji_wpisu_manual(tmp_path):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "1111111111", 5, nip="1111111111", nazwa="Firma A", status="manual")

    sciezka = tmp_path / "kartoteka.xlsx"
    _kartoteka_xlsx(sciezka, [
        [8, "A", "Firma A", "1111111111", "Testowa 1", "00-001", "Warszawa", "1"],
    ])

    raport = rej.synchronizuj_z_kartoteka(PROFIL, sciezka)

    assert rej.pozycja(PROFIL, "1111111111") == 5  # bez zmian
    assert raport.rozjazdy == []
    assert raport.pominiete_reczne == [("1111111111", 5, 8)]
    wpis = {w.klucz: w for w in rej.lista(PROFIL)}["1111111111"]
    assert (wpis.status, wpis.miejscowosc) == ("manual", "")  # nic nie uzupełnione
    ostrzezenia = raport.jako_ostrzezenia()
    assert any(
        "1111111111" in w and "ustawione ręcznie" in w and "było 5" in w and "w Symfonii 8" in w
        for w in ostrzezenia
    )
    rej.close()


def test_sync_nie_uzupelnia_pol_wpisu_manual_na_tej_samej_pozycji(tmp_path):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "1111111111", 1, nip="1111111111", nazwa="", status="manual")

    sciezka = tmp_path / "kartoteka.xlsx"
    _kartoteka_xlsx(sciezka, [
        [1, "A", "Firma A", "1111111111", "Testowa 1", "00-001", "Warszawa", "1"],
    ])

    raport = rej.synchronizuj_z_kartoteka(PROFIL, sciezka)

    assert raport.zaktualizowane == 0
    wpis = {w.klucz: w for w in rej.lista(PROFIL)}["1111111111"]
    assert (wpis.nazwa, wpis.miejscowosc, wpis.pozycja) == ("", "", 1)
    rej.close()


def test_sync_wpis_manual_blokuje_pozycje_dla_klucza_z_kartoteki(tmp_path):
    """Pozycja 3 w Symfonii należy (wg kartoteki) do nowego klucza, ale w
    rejestrze księgowa ręcznie przypisała ją innemu kontrahentowi — wpis
    manual NIE jest ewakuowany, klucz z kartoteki NIE jest dodawany na siłę;
    konflikt trafia do raportu."""
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "RECZNY", 3, nazwa="Ustawiony ręcznie", status="manual")
    rej.ustaw(PROFIL, "2222222222", 4, nip="2222222222", nazwa="Firma B", status="seed")

    sciezka = tmp_path / "kartoteka.xlsx"
    _kartoteka_xlsx(sciezka, [
        [3, "A", "Firma A", "1111111111", "Testowa 1", "00-001", "Warszawa", "1"],
        # B wg FK na 3 też nie może wejść (zajęte), zostaje na 4
        [9, "C", "Firma C", "3333333333", "Polna 3", "00-003", "Gdańsk", "1"],
    ])

    raport = rej.synchronizuj_z_kartoteka(PROFIL, sciezka)

    assert rej.pozycja(PROFIL, "RECZNY") == 3
    assert rej.pozycja(PROFIL, "1111111111") is None  # nie dodany
    assert rej.pozycja(PROFIL, "3333333333") == 9  # reszta synchronizacji działa
    assert raport.dodane == 1
    assert raport.zablokowane == [("1111111111", 3, "RECZNY")]
    assert any(
        "1111111111" in w and "RECZNY" in w and "3" in w for w in raport.jako_ostrzezenia()
    )
    rej.close()


def test_sync_zablokowany_wpis_zostaje_na_miejscu_i_blokuje_kolejny(tmp_path):
    """Łańcuch: M (manual) trzyma 5; B wg FK ma iść na 5 -> zostaje na 7;
    C wg FK ma iść na 7 -> też zablokowany (B zostaje), a nie ewakuuje B."""
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "MMMMMMMMMM", 5, nip="MMMMMMMMMM", nazwa="Ręczny", status="manual")
    rej.ustaw(PROFIL, "2222222222", 7, nip="2222222222", nazwa="Firma B", status="auto")
    rej.ustaw(PROFIL, "3333333333", 8, nip="3333333333", nazwa="Firma C", status="auto")

    sciezka = tmp_path / "kartoteka.xlsx"
    _kartoteka_xlsx(sciezka, [
        [5, "B", "Firma B", "2222222222", "", "", "Kraków", "1"],
        [7, "C", "Firma C", "3333333333", "", "", "Gdańsk", "1"],
    ])

    raport = rej.synchronizuj_z_kartoteka(PROFIL, sciezka)

    assert rej.pozycja(PROFIL, "MMMMMMMMMM") == 5
    assert rej.pozycja(PROFIL, "2222222222") == 7
    assert rej.pozycja(PROFIL, "3333333333") == 8
    assert raport.rozjazdy == []
    assert sorted(raport.zablokowane) == [
        ("2222222222", 5, "MMMMMMMMMM"),
        ("3333333333", 7, "2222222222"),
    ]
    rej.close()


def test_synchronizuj_z_sql_nie_nadpisuje_wpisu_manual(tmp_path, monkeypatch):
    """Ta sama reguła na drodze SQL — to ona chodzi automatycznie przed
    eksportem (_synchronizuj_sql_przed_eksportem w app/gui.py)."""
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "5252445719", 5, nip="5252445719", nazwa="Firma A", status="manual")
    monkeypatch.setattr(
        kontrahenci_nr_mod,
        "_czytaj_kartoteke_sql",
        lambda baza: [_wiersz_sql(8, "Firma A", "5252445719", "Warszawa")],
    )

    raport = rej.synchronizuj_z_sql(PROFIL, "BAZA")

    assert rej.pozycja(PROFIL, "5252445719") == 5
    assert raport.pominiete_reczne == [("5252445719", 5, 8)]
    rej.close()


# ---------------------------------------------------------------------- #
# synchronizuj_z_sql — jak synchronizuj_z_kartoteka, ale źródłem jest żywa
# baza Symfonii (app.symfonia_sql.czytaj_kartoteke), tu monkeypatchowana —
# testuje _synchronizuj_z_fk (rdzeń współdzielony z synchronizuj_z_kartoteka)
# wywołany drogą SQL


def _wiersz_sql(pozycja: int, nazwa: str, nip: str = "", miejscowosc: str = "", kod: str = "") -> dict:
    return {"pozycja": pozycja, "kod": kod, "nazwa": nazwa, "nip": nip, "miejscowosc": miejscowosc}


def test_synchronizuj_z_sql_dodaje_nowe_klucze(tmp_path, monkeypatch):
    monkeypatch.setattr(
        kontrahenci_nr_mod,
        "_czytaj_kartoteke_sql",
        lambda baza: [
            _wiersz_sql(1, "Firma A", "5252445719", "Warszawa"),
            _wiersz_sql(2, "Firma B", "9999999999", "Kraków"),
        ],
    )
    rej = RejestrNumerow(tmp_path / "nr.db")

    raport = rej.synchronizuj_z_sql(PROFIL, "ALFATRON_SERWIS_MEDYCZNY")

    assert raport.dodane == 2
    assert raport.zaktualizowane == 0
    assert raport.rozjazdy == []
    assert raport.brak_w_fk == []
    assert rej.pozycja(PROFIL, "5252445719") == 1
    assert rej.pozycja(PROFIL, "9999999999") == 2
    wpisy = {w.klucz: w for w in rej.lista(PROFIL)}
    assert wpisy["5252445719"].status == "seed"
    rej.close()


def test_synchronizuj_z_sql_rozjazd_nadpisuje_pozycje(tmp_path, monkeypatch):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "5252445719", 5, nip="5252445719", nazwa="Firma A", status="seed")

    monkeypatch.setattr(
        kontrahenci_nr_mod,
        "_czytaj_kartoteke_sql",
        lambda baza: [_wiersz_sql(8, "Firma A", "5252445719", "Warszawa")],
    )

    raport = rej.synchronizuj_z_sql(PROFIL, "ALFATRON_SERWIS_MEDYCZNY")

    assert raport.rozjazdy == [("5252445719", 5, 8)]
    assert rej.pozycja(PROFIL, "5252445719") == 8
    rej.close()


def test_synchronizuj_z_sql_brak_w_fk_nie_usuwa_wpisu(tmp_path, monkeypatch):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "1111111111", 1, nip="1111111111", nazwa="Firma A")
    rej.ustaw(PROFIL, "ZDUPLIKOWANY", 2, nazwa="Karta-duplikat scalona aliasem")

    monkeypatch.setattr(
        kontrahenci_nr_mod,
        "_czytaj_kartoteke_sql",
        lambda baza: [_wiersz_sql(1, "Firma A", "1111111111", "Warszawa")],
    )

    raport = rej.synchronizuj_z_sql(PROFIL, "ALFATRON_SERWIS_MEDYCZNY")

    assert raport.brak_w_fk == [("ZDUPLIKOWANY", 2)]
    assert rej.pozycja(PROFIL, "ZDUPLIKOWANY") == 2
    rej.close()


def test_synchronizuj_z_sql_woła_czytnik_z_nazwa_bazy(tmp_path, monkeypatch):
    """Nazwa bazy przekazana do synchronizuj_z_sql trafia bez zmian do
    czytnika (app.symfonia_sql.czytaj_kartoteke)."""
    przekazana_baza = []

    def fake_czytnik(baza):
        przekazana_baza.append(baza)
        return []

    monkeypatch.setattr(kontrahenci_nr_mod, "_czytaj_kartoteke_sql", fake_czytnik)
    rej = RejestrNumerow(tmp_path / "nr.db")

    rej.synchronizuj_z_sql(PROFIL, "ALFATRON_SERWIS_MEDYCZNY")

    assert przekazana_baza == ["ALFATRON_SERWIS_MEDYCZNY"]
    rej.close()


def test_synchronizuj_z_sql_duplikat_klucza_w_zrodle_pomija_z_ostrzezeniem(tmp_path, monkeypatch):
    """Dwa wiersze kartoteki SQL z tym samym NIP-em (ten sam klucz) -> drugi
    pominięty z ostrzeżeniem, tak jak przy duplikacie klucza w pliku."""
    monkeypatch.setattr(
        kontrahenci_nr_mod,
        "_czytaj_kartoteke_sql",
        lambda baza: [
            _wiersz_sql(1, "Firma A", "5252445719", "Warszawa"),
            _wiersz_sql(2, "Firma A (oddział)", "5252445719", "Kraków"),
        ],
    )
    rej = RejestrNumerow(tmp_path / "nr.db")

    raport = rej.synchronizuj_z_sql(PROFIL, "ALFATRON_SERWIS_MEDYCZNY")

    assert raport.dodane == 1
    assert rej.pozycja(PROFIL, "5252445719") == 1
    assert any("zduplikowany" in o for o in raport.ostrzezenia)


# ---------------------------------------------------------------------- #
# BŁĄD PRODUKCYJNY (2026-08-25): synchronizuj_z_sql liczyła klucz z SUROWEGO
# NIP-u (tylko .strip()-owanego przez app.symfonia_sql.czytaj_kartoteke), więc
# NIP z prefiksem 'PL' albo zagraniczny VAT-UE trafiał do rejestru pod
# kluczem innym niż kanoniczny (_klucz_seed po _digits, jak w plikowym
# seedzie/emisji hmffk) — rozjazd kluczy między drogą SQL a resztą aplikacji
# powodował duplikaty kart przy kolejnych fakturach.


def test_synchronizuj_z_sql_nip_z_prefiksem_pl_dostaje_klucz_kanoniczny(tmp_path, monkeypatch):
    """Wiersz SQL z nip='PL9100010037' -> klucz i nip w wpisie to SAME CYFRY
    ('9100010037'), zgodnie z _klucz_seed/_digits — nie surowy 'PL9100010037'."""
    rej = RejestrNumerow(tmp_path / "nr.db")
    # istniejący wpis pod kluczem KANONICZNYM (np. z seedu plikowego wcześniej)
    rej.ustaw(PROFIL, "9100010037", 5, nip="9100010037", nazwa="Firma A", status="seed")

    monkeypatch.setattr(
        kontrahenci_nr_mod,
        "_czytaj_kartoteke_sql",
        lambda baza: [_wiersz_sql(8, "Firma A", "PL9100010037", "Warszawa")],
    )

    raport = rej.synchronizuj_z_sql(PROFIL, "ALFATRON_SERWIS_MEDYCZNY")

    # dopasowany do ISTNIEJĄCEGO wpisu (aktualizacja pozycji) — NIE nowy klucz,
    # NIE zgłoszony jako brak_w_fk
    assert raport.dodane == 0
    assert raport.rozjazdy == [("9100010037", 5, 8)]
    assert raport.brak_w_fk == []
    assert rej.pozycja(PROFIL, "9100010037") == 8
    assert rej.pozycja(PROFIL, "PL9100010037") is None
    wpis = {w.klucz: w for w in rej.lista(PROFIL)}["9100010037"]
    assert wpis.nip == "9100010037"
    rej.close()


def test_synchronizuj_z_sql_zagraniczny_vat_klucz_z_samych_cyfr(tmp_path, monkeypatch):
    """NIP zagraniczny z literami w środku (VAT-UE NL) -> klucz to same cyfry,
    dokładnie jak dałoby _klucz_seed(nazwa, _digits(nip), miejscowosc)."""
    nip_surowy = "NL005113989B90"
    oczekiwany_klucz = kontrahenci_nr_mod._klucz_seed(
        "Firma NL", _digits(nip_surowy), "Amsterdam"
    )
    assert oczekiwany_klucz == _digits(nip_surowy)  # sama derywacja - bez liter

    monkeypatch.setattr(
        kontrahenci_nr_mod,
        "_czytaj_kartoteke_sql",
        lambda baza: [_wiersz_sql(1, "Firma NL", nip_surowy, "Amsterdam")],
    )
    rej = RejestrNumerow(tmp_path / "nr.db")

    raport = rej.synchronizuj_z_sql(PROFIL, "ALFATRON_SERWIS_MEDYCZNY")

    assert raport.dodane == 1
    assert rej.pozycja(PROFIL, oczekiwany_klucz) == 1
    wpis = {w.klucz: w for w in rej.lista(PROFIL)}[oczekiwany_klucz]
    assert wpis.nip == _digits(nip_surowy)
    rej.close()


def test_synchronizuj_z_sql_bez_nip_klucz_hash_zgodny_z_klucz_seed(tmp_path, monkeypatch):
    """Wiersz SQL bez NIP-u -> klucz K_<hash> BAJT W BAJT taki sam jak zwraca
    _klucz_seed dla tej samej nazwy/miejscowości (spójność z plikowym seedem)."""
    nazwa = "Kontrahent Bez Nipu"
    miejscowosc = "Poznań"
    oczekiwany_klucz = _klucz_hash(nazwa, miejscowosc)
    assert oczekiwany_klucz == kontrahenci_nr_mod._klucz_seed(nazwa, "", miejscowosc)

    monkeypatch.setattr(
        kontrahenci_nr_mod,
        "_czytaj_kartoteke_sql",
        lambda baza: [_wiersz_sql(1, nazwa, "", miejscowosc)],
    )
    rej = RejestrNumerow(tmp_path / "nr.db")

    raport = rej.synchronizuj_z_sql(PROFIL, "ALFATRON_SERWIS_MEDYCZNY")

    assert raport.dodane == 1
    assert rej.pozycja(PROFIL, oczekiwany_klucz) == 1
    rej.close()


def test_synchronizuj_z_sql_i_plikowy_seed_daja_identyczne_klucze(tmp_path, monkeypatch):
    """REGRESJA: te same dane źródłowe (NIP z prefiksem PL, zagraniczny VAT-UE,
    brak NIP-u) przepuszczone przez ścieżkę plikową (synchronizuj_z_kartoteka)
    i przez ścieżkę SQL (synchronizuj_z_sql) muszą dać IDENTYCZNE klucze."""
    wiersze_zrodlowe = [
        (1, "Firma PL", "PL9100010037", "Warszawa"),
        (2, "Firma NL", "NL005113989B90", "Amsterdam"),
        (3, "Kontrahent Bez Nipu", "", "Poznań"),
    ]

    sciezka = tmp_path / "kartoteka.xlsx"
    _kartoteka_xlsx(
        sciezka,
        [
            [poz, "K", nazwa, nip, "Adres", "00-000", miejscowosc, "1"]
            for poz, nazwa, nip, miejscowosc in wiersze_zrodlowe
        ],
    )
    rej_plik = RejestrNumerow(tmp_path / "nr_plik.db")
    rej_plik.synchronizuj_z_kartoteka(PROFIL, sciezka)
    klucze_plik = sorted(w.klucz for w in rej_plik.lista(PROFIL))
    rej_plik.close()

    monkeypatch.setattr(
        kontrahenci_nr_mod,
        "_czytaj_kartoteke_sql",
        lambda baza: [
            _wiersz_sql(poz, nazwa, nip, miejscowosc)
            for poz, nazwa, nip, miejscowosc in wiersze_zrodlowe
        ],
    )
    rej_sql = RejestrNumerow(tmp_path / "nr_sql.db")
    rej_sql.synchronizuj_z_sql(PROFIL, "ALFATRON_SERWIS_MEDYCZNY")
    klucze_sql = sorted(w.klucz for w in rej_sql.lista(PROFIL))
    rej_sql.close()

    assert klucze_plik == klucze_sql
    assert klucze_plik == sorted(
        [
            "9100010037",
            _digits("NL005113989B90"),
            _klucz_hash("Kontrahent Bez Nipu", "Poznań"),
        ]
    )


# ---------------------------------------------------------------------- #
# BEZPIECZNIK (incydent produkcyjny 2026-08-25, profil ALFATRON): kartoteka
# ze ŹLE wybranej bazy/pliku (INNEJ firmy) nie może dopisać/nadpisać rejestru
# — patrz SynchronizacjaOdrzucona i RejestrNumerow._sprawdz_zgodnosc_z_fk.


def _wpelnij_profil_nipami(rej: RejestrNumerow, ile: int) -> None:
    """Zasila profil PROFIL `ile` wpisami z unikalnym 10-cyfrowym NIP-em każdy —
    do testów progu kontroli zgodności (>= 20 wpisów z NIP-em włącza kontrolę)."""
    for i in range(ile):
        nip = f"{1000000000 + i}"
        rej.ustaw(PROFIL, nip, i + 1, nip=nip, nazwa=f"Firma {i}", status="seed")


def _stan_rejestru(rej: RejestrNumerow) -> list[tuple]:
    """Zrzut całego stanu profilu PROFIL do porównania przed/po — "bajt w
    bajt bez zmian" przy odrzuconej synchronizacji."""
    return [
        (w.klucz, w.pozycja, w.nazwa, w.nip, w.miejscowosc, w.status)
        for w in rej.lista(PROFIL)
    ]


def test_sync_bezpiecznik_odrzuca_kartoteke_innej_firmy(tmp_path):
    """(a) profil z 30 wpisami NIP, kartoteka innej firmy (jeden wspólny NIP
    na 30 -> ok. 3%, poniżej progu 60%) -> SynchronizacjaOdrzucona, rejestr
    bez ŻADNEJ zmiany (porównanie stanu przed/po)."""
    rej = RejestrNumerow(tmp_path / "nr.db")
    _wpelnij_profil_nipami(rej, 30)
    stan_przed = _stan_rejestru(rej)

    sciezka = tmp_path / "obca_kartoteka.xlsx"
    wiersze = [[1, "X", "Obca Firma 1", "1000000000", "Adres 1", "00-000", "Innowo", "1"]]
    for j in range(2, 10):
        wiersze.append(
            [j, "X", f"Obca Firma {j}", f"{9000000000 + j}", "Adres", "00-000", "Innowo", "1"]
        )
    _kartoteka_xlsx(sciezka, wiersze)

    with pytest.raises(SynchronizacjaOdrzucona) as exc_info:
        rej.synchronizuj_z_kartoteka(PROFIL, sciezka)

    komunikat = str(exc_info.value)
    assert "1 z 30" in komunikat
    assert "INNEJ firmy" in komunikat
    assert "Wyczyść rejestr profilu" in komunikat

    assert _stan_rejestru(rej) == stan_przed  # rejestr bajt w bajt bez zmian
    rej.close()


def test_sync_bezpiecznik_przechodzi_gdy_pelna_zgodnosc(tmp_path):
    """(b) zgodność 100% (wszystkie NIP-y profilu obecne w kartotece) ->
    kontrola przepuszcza, synchronizacja przebiega normalnie."""
    rej = RejestrNumerow(tmp_path / "nr.db")
    _wpelnij_profil_nipami(rej, 20)

    sciezka = tmp_path / "wlasna_kartoteka.xlsx"
    wiersze = [
        [i + 1, "X", f"Firma {i}", f"{1000000000 + i}", "Adres", "00-000", "Warszawa", "1"]
        for i in range(20)
    ]
    _kartoteka_xlsx(sciezka, wiersze)

    raport = rej.synchronizuj_z_kartoteka(PROFIL, sciezka)  # nie może rzucić

    assert raport.dodane == 0
    rej.close()


def test_sync_bezpiecznik_pomija_kontrole_gdy_profil_pusty(tmp_path):
    """(c) profil pusty -> bez kontroli (seed startowy ma przechodzić), mimo
    że kartoteka nie ma z profilem nic wspólnego (bo nic jeszcze nie ma)."""
    rej = RejestrNumerow(tmp_path / "nr.db")

    sciezka = tmp_path / "seed.xlsx"
    _kartoteka_xlsx(sciezka, [
        [1, "X", "Zupełnie inna firma", "9999999999", "Adres", "00-000", "Gdansk", "1"],
    ])

    raport = rej.synchronizuj_z_kartoteka(PROFIL, sciezka)  # nie może rzucić

    assert raport.dodane == 1
    rej.close()


def test_sync_bezpiecznik_pomija_kontrole_ponizej_progu_wpisow(tmp_path):
    """(d) profil z 10 wpisami NIP (< 20 -> poniżej progu kontroli) ->
    kartoteka zupełnie innej firmy i tak przechodzi bez wyjątku."""
    rej = RejestrNumerow(tmp_path / "nr.db")
    _wpelnij_profil_nipami(rej, 10)

    sciezka = tmp_path / "obca_kartoteka.xlsx"
    _kartoteka_xlsx(sciezka, [
        [1, "X", "Zupełnie inna firma", "9999999999", "Adres", "00-000", "Gdansk", "1"],
    ])

    raport = rej.synchronizuj_z_kartoteka(PROFIL, sciezka)  # nie może rzucić

    assert raport.dodane == 1
    rej.close()


def test_sync_bezpiecznik_wymus_omija_kontrole(tmp_path):
    """(e) wymus=True świadomie pomija kontrolę zgodności — nawet kartoteka
    innej firmy (0% zgodności) zostaje zsynchronizowana."""
    rej = RejestrNumerow(tmp_path / "nr.db")
    _wpelnij_profil_nipami(rej, 30)

    sciezka = tmp_path / "obca_kartoteka.xlsx"
    _kartoteka_xlsx(sciezka, [
        [100, "X", "Zupełnie inna firma", "9999999999", "Adres", "00-000", "Gdansk", "1"],
    ])

    raport = rej.synchronizuj_z_kartoteka(PROFIL, sciezka, wymus=True)  # nie może rzucić

    assert raport.dodane == 1
    assert rej.pozycja(PROFIL, "9999999999") == 100
    rej.close()


def test_synchronizuj_z_sql_bezpiecznik_odrzuca_gdy_niska_zgodnosc(tmp_path, monkeypatch):
    """Bezpiecznik działa tak samo na drodze SQL (synchronizuj_z_sql) — to
    właśnie ta droga (auto-sync przed eksportem) doprowadziła do incydentu."""
    rej = RejestrNumerow(tmp_path / "nr.db")
    _wpelnij_profil_nipami(rej, 30)
    stan_przed = _stan_rejestru(rej)

    monkeypatch.setattr(
        kontrahenci_nr_mod,
        "_czytaj_kartoteke_sql",
        lambda baza: [_wiersz_sql(1, "Obca Firma", "9999999999", "Innowo")],
    )

    with pytest.raises(SynchronizacjaOdrzucona):
        rej.synchronizuj_z_sql(PROFIL, "ALFATRON_SERWIS_MEDYCZNY")

    assert _stan_rejestru(rej) == stan_przed
    rej.close()


# RaportSync.jako_ostrzezenia — spłaszczenie rozjazdów/brak_w_fk/ostrzezenia
# do listy tekstowej, używane przy auto-synchronizacji przed eksportem
# (app/gui.py ExportWorker, app/cli.py --sync-sql)

def test_sync_raport_liczy_wpisy_manual(tmp_path):
    """(12) RaportSync.reczne — ile wpisów ze statusem manual synchronizacja
    pominęła w całości (nie zmienia ich w żaden sposób)."""
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "1111111111", 5, nip="1111111111", nazwa="Firma A", status="manual")
    rej.ustaw(PROFIL, "2222222222", 2, nip="2222222222", nazwa="Firma B", status="manual")
    rej.ustaw(PROFIL, "3333333333", 3, nip="3333333333", nazwa="Firma C", status="seed")
    sciezka = tmp_path / "kartoteka.xlsx"
    _kartoteka_xlsx(sciezka, [
        [8, "A", "Firma A", "1111111111", "", "", "Warszawa", "1"],
        [2, "B", "Firma B", "2222222222", "", "", "Kraków", "1"],
        [3, "C", "Firma C", "3333333333", "", "", "Gdańsk", "1"],
    ])

    raport = rej.synchronizuj_z_kartoteka(PROFIL, sciezka)

    assert raport.reczne == 2
    assert raport.pominiete_reczne == [("1111111111", 5, 8)]
    assert {w.klucz: w.status for w in rej.lista(PROFIL)} == {
        "1111111111": "manual", "2222222222": "manual", "3333333333": "seed",
    }  # dane NIE zmieniane automatycznie
    rej.close()


def test_jako_ostrzezenia_podpowiada_jak_odblokowac_wpisy_manual():
    from app.kontrahenci_nr import RaportSync

    raport = RaportSync(
        dodane=0, zaktualizowane=0, rozjazdy=[], brak_w_fk=[], ostrzezenia=[],
        pominiete_reczne=[("1111111111", 5, 8)], reczne=3,
    )
    wynik = raport.jako_ostrzezenia()
    podpowiedz = [w for w in wynik if "seed" in w]
    assert len(podpowiedz) == 1
    assert "3" in podpowiedz[0] and "manual" in podpowiedz[0] and "Kontrahenci" in podpowiedz[0]


def test_jako_ostrzezenia_bez_roznic_manual_nie_dodaje_podpowiedzi():
    """Same wpisy manual bez różnicy z Symfonią to nie problem — raport eksportu
    (auto-synchronizacja przed każdym eksportem) nie może nimi szumieć."""
    from app.kontrahenci_nr import RaportSync

    raport = RaportSync(dodane=0, zaktualizowane=0, rozjazdy=[], brak_w_fk=[], ostrzezenia=[], reczne=5)
    assert raport.jako_ostrzezenia() == []


def test_jako_ostrzezenia_puste_gdy_brak_problemow():
    from app.kontrahenci_nr import RaportSync

    raport = RaportSync(dodane=2, zaktualizowane=1, rozjazdy=[], brak_w_fk=[], ostrzezenia=[])
    assert raport.jako_ostrzezenia() == []


def test_jako_ostrzezenia_laczy_rozjazdy_brak_w_fk_i_ostrzezenia():
    from app.kontrahenci_nr import RaportSync

    raport = RaportSync(
        dodane=0,
        zaktualizowane=0,
        rozjazdy=[("5252445719", 5, 8)],
        brak_w_fk=[("ZDUPLIKOWANY", 2)],
        ostrzezenia=["Wiersz 3: coś nie tak"],
    )
    wynik = raport.jako_ostrzezenia()
    assert len(wynik) == 3
    assert any("5252445719" in w and "5" in w and "8" in w for w in wynik)
    assert any("ZDUPLIKOWANY" in w and "pozycja 2" in w for w in wynik)
    assert any("Wiersz 3: coś nie tak" in w for w in wynik)


def test_sync_z_pliku_zapisuje_kod_karty(tmp_path):
    """Kolumna Kod z eksportu kartoteki FK trafia do rejestru (skrot) — eksport
    wysyła ją jako kod istniejącej karty (decyzja 29.09)."""
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "5252445719", 1, nip="5252445719", nazwa="Firma A", status="seed")
    sciezka = tmp_path / "kartoteka.xlsx"
    _kartoteka_xlsx(sciezka, [
        [1, "FIRMA-A", "Firma A", "5252445719", "Testowa 1", "00-001", "Warszawa", "1"],
    ])

    rej.synchronizuj_z_kartoteka(PROFIL, sciezka)

    assert rej.wpis(PROFIL, "5252445719").skrot == "FIRMA-A"
    rej.close()
