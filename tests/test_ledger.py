"""Testy rejestru dedup."""

from app.ledger import Ledger


def test_dedup_roundtrip(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    assert not ledger.is_done("5252445719", "NR-1")
    ledger.mark_done("5252445719", "NR-1", r"C:\out\NR-1.TXT")
    assert ledger.is_done("5252445719", "NR-1")
    assert not ledger.is_done("1180012345", "NR-1")  # inny NIP = osobny rejestr
    assert ledger.count("5252445719") == 1
    ledger.close()

    reopened = Ledger(tmp_path / "ledger.db")
    assert reopened.is_done("5252445719", "NR-1")
    reopened.close()


def test_mark_done_idempotent(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.mark_done("5252445719", "NR-1", "a")
    ledger.mark_done("5252445719", "NR-1", "b")
    assert ledger.count("5252445719") == 1
    ledger.close()


def test_punkt_kontynuacji_trwaly_per_nip_i_rodzaj(tmp_path):
    """Punkt kontynuacji synchronizacji (HWM z KSeF) przeżywa zamknięcie
    aplikacji i jest osobny dla każdej firmy (NIP) i rodzaju (Subject1/2)."""
    ledger = Ledger(tmp_path / "ledger.db")
    assert ledger.sync_point("5252445719", "Subject2") is None
    ledger.set_sync_point("5252445719", "Subject2", "2026-08-05T10:00:00.1234567+00:00")
    ledger.set_sync_point("5252445719", "Subject1", "2026-08-01T00:00:00+02:00")
    ledger.set_sync_point("1180012345", "Subject2", "2026-07-01T00:00:00+02:00")
    ledger.close()

    reopened = Ledger(tmp_path / "ledger.db")
    # wartość zapisana DOKŁADNIE tak, jak przyszła z KSeF (bez utraty precyzji)
    assert reopened.sync_point("5252445719", "Subject2") == "2026-08-05T10:00:00.1234567+00:00"
    assert reopened.sync_point("5252445719", "Subject1") == "2026-08-01T00:00:00+02:00"
    assert reopened.sync_point("1180012345", "Subject2") == "2026-07-01T00:00:00+02:00"
    assert reopened.sync_point("1180012345", "Subject1") is None
    reopened.set_sync_point("5252445719", "Subject2", "2026-08-06T09:00:00+00:00")
    assert reopened.sync_point("5252445719", "Subject2") == "2026-08-06T09:00:00+00:00"
    # rejestr pobranych faktur bez zmian
    assert reopened.count("5252445719") == 0
    reopened.close()


# --------------------------------------------------------------------------- #
# W2 (28.09): zapamiętane zakresy eksportu — per (NIP, rodzaj), trwale

from datetime import date  # noqa: E402

NIP_A, NIP_B = "5252445719", "1180012345"


def test_zakres_eksportu_trwaly_per_nip_i_rodzaj(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    assert not ledger.is_covered(NIP_A, "Subject2", date(2026, 9, 3))
    ledger.record_range(NIP_A, "Subject2", date(2026, 9, 1), date(2026, 9, 30))
    ledger.record_range(NIP_A, "Subject2", date(2026, 9, 1), date(2026, 9, 30))  # idempotentnie
    ledger.close()

    led = Ledger(tmp_path / "ledger.db")
    for d in (date(2026, 9, 1), date(2026, 9, 3), date(2026, 9, 30)):
        assert led.is_covered(NIP_A, "Subject2", d)
    assert not led.is_covered(NIP_A, "Subject2", date(2026, 8, 31))
    assert not led.is_covered(NIP_A, "Subject2", date(2026, 10, 1))
    assert not led.is_covered(NIP_A, "Subject1", date(2026, 9, 3))  # inny rodzaj
    assert not led.is_covered(NIP_B, "Subject2", date(2026, 9, 3))  # inna firma
    assert led.ranges(NIP_A, "Subject2") == [(date(2026, 9, 1), date(2026, 9, 30))]
    led.close()


def test_miesiac_domyka_tylko_jeden_zakres_calego_miesiaca(tmp_path):
    """Decyzja właściciela (28.09): miesiąc jest „wyeksportowany” dopiero po
    eksporcie CAŁEGO miesiąca jednym zakresem (domknięcie). Zakresy „na
    bieżąco” go nie domykają — także gdy razem pokrywają cały miesiąc."""
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.record_range(NIP_A, "Subject1", date(2026, 9, 1), date(2026, 9, 7))
    assert not ledger.is_covered(NIP_A, "Subject1", date(2026, 9, 3))
    ledger.record_range(NIP_A, "Subject1", date(2026, 9, 8), date(2026, 9, 15))
    ledger.record_range(NIP_A, "Subject1", date(2026, 9, 16), date(2026, 9, 30))
    assert not ledger.is_covered(NIP_A, "Subject1", date(2026, 9, 3))  # suma częściowych — nie
    ledger.record_range(NIP_A, "Subject1", date(2026, 9, 1), date(2026, 9, 30))
    assert ledger.is_covered(NIP_A, "Subject1", date(2026, 9, 3))
    ledger.close()


def test_zakres_wielomiesieczny_domyka_kazdy_w_pelni_objety_miesiac(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.record_range(NIP_A, "Subject2", date(2026, 7, 15), date(2026, 9, 30))
    assert not ledger.is_covered(NIP_A, "Subject2", date(2026, 7, 20))  # lipiec od 15. — nie
    assert ledger.is_covered(NIP_A, "Subject2", date(2026, 8, 1))
    assert ledger.is_covered(NIP_A, "Subject2", date(2026, 9, 30))
    ledger.record_range(NIP_A, "Subject2", date(2026, 1, 1), date(2026, 3, 31))
    assert ledger.is_covered(NIP_A, "Subject2", date(2026, 2, 28))  # luty (28 dni)
    ledger.record_range(NIP_A, "Subject1", date(2028, 2, 1), date(2028, 2, 28))
    assert not ledger.is_covered(NIP_A, "Subject1", date(2028, 2, 10))  # 2028 przestępny: do 29.02
    ledger.close()


# --------------------------------------------------------------------------- #
# W3: spóźnione dołączone do eksportu — do którego zakresu, trwale


def _dolacz(ledger, nr, od, do, *, status="w_pliku", blad=None, rodzaj="Subject2"):
    ledger.record_late(
        NIP_A, rodzaj, nr, invoice_number=f"FV/{nr}", issue_date="2026-08-28",
        acquisition_date="2026-09-10T08:00:00+00:00",
        permanent_storage_date="2026-09-10T08:00:00+00:00",
        range_from=od, range_to=do, status=status, error=blad,
    )


def test_spozniona_zapamietuje_zakres_do_ktorego_ja_dolaczono(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    _dolacz(ledger, "SP-1", date(2026, 9, 8), date(2026, 9, 15))
    _dolacz(ledger, "SP-1", date(2026, 9, 16), date(2026, 9, 30))  # pierwsze dołączenie wygrywa
    _dolacz(ledger, "SP-2", date(2026, 9, 16), date(2026, 9, 30))
    ledger.close()

    led = Ledger(tmp_path / "ledger.db")
    # ten sam zakres albo zakres go obejmujący (domknięcie miesiąca)
    assert [r.ksef_number for r in led.late_within(NIP_A, "Subject2", date(2026, 9, 8), date(2026, 9, 15))] == ["SP-1"]
    assert sorted(r.ksef_number for r in led.late_within(NIP_A, "Subject2", date(2026, 9, 1), date(2026, 9, 30))) == ["SP-1", "SP-2"]
    # zakres tylko częściowo nachodzący na zakres dołączenia — nie (SP-1 był w 08–15.09)
    assert [r.ksef_number for r in led.late_within(NIP_A, "Subject2", date(2026, 9, 10), date(2026, 9, 30))] == ["SP-2"]
    assert led.late_within(NIP_A, "Subject1", date(2026, 9, 1), date(2026, 9, 30)) == []
    [rec] = led.late_within(NIP_A, "Subject2", date(2026, 9, 8), date(2026, 9, 15))
    assert (rec.invoice_number, rec.issue_date, rec.status) == ("FV/SP-1", "2026-08-28", "w_pliku")
    assert rec.acquisition_date == "2026-09-10T08:00:00+00:00"
    led.close()


def test_spozniona_z_bledem_mapowania_na_trwalej_liscie_do_recznej_obslugi(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    _dolacz(ledger, "SP-X", date(2026, 9, 8), date(2026, 9, 15),
            status="do_recznej_obslugi", blad="brak P_15")
    [rec] = ledger.manual_list(NIP_A)
    assert (rec.ksef_number, rec.status, rec.error) == ("SP-X", "do_recznej_obslugi", "brak P_15")
    # później (np. po poprawce aplikacji) trafiła do pliku — status się zmienia, zakres zostaje
    _dolacz(ledger, "SP-X", date(2026, 10, 1), date(2026, 10, 7))
    assert ledger.manual_list(NIP_A) == []
    [rec] = ledger.late_within(NIP_A, "Subject2", date(2026, 9, 8), date(2026, 9, 15))
    assert rec.status == "w_pliku"
    ledger.close()


# --------------------------------------------------------------------------- #
# punkt kontynuacji: moment rozpoczęcia obserwacji (do bezpiecznika 60 dni)


def test_pierwszy_punkt_zapamietany_jako_poczatek_obserwacji(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    assert ledger.watch_from(NIP_A, "Subject2") is None
    ledger.set_sync_point(NIP_A, "Subject2", "2026-09-01T10:00:00+00:00")
    ledger.set_sync_point(NIP_A, "Subject2", "2026-09-20T10:00:00+00:00")
    ledger.close()
    led = Ledger(tmp_path / "ledger.db")
    assert led.sync_point(NIP_A, "Subject2") == "2026-09-20T10:00:00+00:00"
    assert led.watch_from(NIP_A, "Subject2") == "2026-09-01T10:00:00+00:00"
    assert led.watch_from(NIP_A, "Subject1") is None
    led.close()


def test_poczatek_obserwacji_podany_jawnie(tmp_path):
    """Punkt bywa cofnięty (wstrzymana faktura) — początkiem obserwacji jest
    wtedy moment sprawdzenia (HWM), podawany osobno."""
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.set_sync_point(NIP_A, "Subject2", "2026-08-01T10:00:00+00:00",
                          observed_at="2026-08-16T06:00:00+00:00")
    ledger.set_sync_point(NIP_A, "Subject2", "2026-09-01T10:00:00+00:00",
                          observed_at="2026-09-02T06:00:00+00:00")
    assert ledger.watch_from(NIP_A, "Subject2") == "2026-08-16T06:00:00+00:00"
    ledger.close()


def test_stara_tabela_punktow_bez_kolumny_poczatku_obserwacji(tmp_path):
    """Baza z wersji 42c0b15 (sync_points bez watch_from) otwiera się bez błędu."""
    import sqlite3

    sciezka = tmp_path / "ledger.db"
    conn = sqlite3.connect(str(sciezka))
    conn.execute(
        "CREATE TABLE sync_points(nip TEXT NOT NULL, subject_type TEXT NOT NULL, "
        "point TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (nip, subject_type))"
    )
    conn.execute("INSERT INTO sync_points VALUES (?, ?, ?, ?)",
                 (NIP_A, "Subject2", "2026-09-01T10:00:00+00:00", "x"))
    conn.commit()
    conn.close()

    led = Ledger(sciezka)
    assert led.sync_point(NIP_A, "Subject2") == "2026-09-01T10:00:00+00:00"
    assert led.watch_from(NIP_A, "Subject2") == "2026-09-01T10:00:00+00:00"  # najlepsze przybliżenie
    led.set_sync_point(NIP_A, "Subject2", "2026-09-05T10:00:00+00:00")
    assert led.watch_from(NIP_A, "Subject2") == "2026-09-01T10:00:00+00:00"
    led.close()


# --------------------------------------------------------------------------- #
# (10) nowi kontrahenci: ostrzeżenie czeka w rejestrze, aż trafi do raportu


def test_nowi_kontrahenci_czekaja_do_odebrania(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.add_pending_contractors(NIP_A, [("Dostawca A", "1180012345", 7, "202-2-7")])
    ledger.add_pending_contractors(NIP_A, [("Dostawca A", "1180012345", 7, "202-2-7"),
                                           ("Klient B", "", 8, "201-2-8")])
    ledger.close()
    led = Ledger(tmp_path / "ledger.db")
    assert led.take_pending_contractors(NIP_B) == []
    assert led.take_pending_contractors(NIP_A) == [
        ("Dostawca A", "1180012345", 7, "202-2-7"), ("Klient B", "", 8, "201-2-8"),
    ]
    assert led.take_pending_contractors(NIP_A) == []  # odebrane raz
    led.close()


# --------------------------------------------------------------------------- #
# przejście ze starszej wersji: zakresy odtworzone z nazw plików w rejestrze


def test_zakresy_odtworzone_z_nazw_plikow_starszej_wersji(tmp_path):
    import sqlite3

    sciezka = tmp_path / "ledger.db"
    conn = sqlite3.connect(str(sciezka))
    conn.execute(
        "CREATE TABLE exports(nip TEXT NOT NULL, ksef_number TEXT NOT NULL, "
        "txt_path TEXT NOT NULL, exported_at TEXT NOT NULL, PRIMARY KEY (nip, ksef_number))"
    )
    wiersze = [
        # hmffk: rodzaj z numeru KSeF (zaczyna się NIP-em sprzedawcy)
        (NIP_A, f"{NIP_A}-20260805-AAAA-01", r"C:\eks\firma_a\hmffk_firma_a_2026-08-01_2026-08-31_20260912-101500.TXT"),
        (NIP_A, "1180012345-20260806-BBBB-02", r"C:\eks\firma_a\hmffk_firma_a_2026-08-01_2026-08-31_20260912-101500.TXT"),
        # tylko koszty w pliku 01-07.09
        (NIP_A, "1180012345-20260903-CCCC-03", r"C:\eks\firma_a\hmffk_firma_a_2026-09-01_2026-09-07_20260908-090000.TXT"),
        # paczka sksef: rodzaj z nazwy
        (NIP_A, "9999999999-20260910-DDDD-04", "/eks/firma_a/paczka_koszt_2026-09-08_2026-09-15_20260916-080000.TXT"),
        # wyszukiwanie po numerze / kontrahencie — to NIE jest eksport zakresu
        (NIP_A, "9999999999-20260920-EEEE-05", "/eks/firma_a/hmffk_firma_a_numery_2026-09-16_2026-09-30_20261001-080000.TXT"),
        (NIP_A, "9999999999-20260921-FFFF-06", "/eks/firma_a/paczka_koszt_kontrahenci_2026-09-16_2026-09-30_20261001-080000.TXT"),
        # plik per faktura — bez zakresu
        (NIP_A, "9999999999-20260922-GGGG-07", "/eks/firma_a/9999999999-20260922-GGGG-07.TXT"),
    ]
    conn.executemany("INSERT INTO exports VALUES (?, ?, ?, 'x')", wiersze)
    conn.commit()
    conn.close()

    led = Ledger(sciezka)
    assert led.ranges(NIP_A, "Subject1") == [(date(2026, 8, 1), date(2026, 8, 31))]
    # ta sama reguła odczytu: plik całego sierpnia domyka sierpień, 01–07.09 — nie
    assert led.is_covered(NIP_A, "Subject2", date(2026, 8, 20))
    assert not led.is_covered(NIP_A, "Subject2", date(2026, 9, 3))
    assert led.ranges(NIP_A, "Subject2") == [
        (date(2026, 8, 1), date(2026, 8, 31)),
        (date(2026, 9, 1), date(2026, 9, 7)),
        (date(2026, 9, 8), date(2026, 9, 15)),
    ]
    led.close()
    # jednorazowo: ponowne otwarcie nie dopisuje niczego
    led = Ledger(sciezka)
    assert len(led.ranges(NIP_A, "Subject2")) == 3
    led.close()


# --------------------------------------------------------------------------- #
# runda 3 (N2/N3): przejście ze starszej wersji


def _stara_baza(sciezka, wiersze):
    """ledger.db starszej wersji: sama tabela exports (nip, nr, ścieżka, exported_at)."""
    import sqlite3

    conn = sqlite3.connect(str(sciezka))
    conn.execute(
        "CREATE TABLE exports(nip TEXT NOT NULL, ksef_number TEXT NOT NULL, "
        "txt_path TEXT NOT NULL, exported_at TEXT NOT NULL, PRIMARY KEY (nip, ksef_number))"
    )
    conn.executemany("INSERT INTO exports VALUES (?, ?, ?, ?)", wiersze)
    conn.commit()
    conn.close()


def test_migracja_przycina_zakres_do_dnia_przed_stemplem_pliku(tmp_path):
    """N2: plik 01–30.09 zrobiony 16.09 („Do” = koniec miesiąca w trakcie
    miesiąca) obejmuje naprawdę tylko 01–15.09 — nie domyka września. Zakres
    zrobiony tuż po północy 1. dnia (stempel − 1 < od) — nic."""
    k = "1180012345-20260903-CCCC-03"
    _stara_baza(tmp_path / "ledger.db", [
        (NIP_A, k, r"C:\eks\a\hmffk_a_2026-09-01_2026-09-30_20260916-101500.TXT", "2026-09-16T08:15:00+00:00"),
        (NIP_A, "1180012345-20261001-DDDD-04",
         r"C:\eks\a\hmffk_a_2026-10-01_2026-10-31_20261001-000500.TXT", "2026-09-30T22:05:00+00:00"),
        (NIP_A, "1180012345-20260805-EEEE-05",
         r"C:\eks\a\hmffk_a_2026-08-01_2026-08-31_20260910-100000.TXT", "2026-09-10T08:00:00+00:00"),
    ])
    led = Ledger(tmp_path / "ledger.db")
    assert led.ranges(NIP_A, "Subject2") == [
        (date(2026, 8, 1), date(2026, 8, 31)), (date(2026, 9, 1), date(2026, 9, 15)),
    ]
    assert not led.is_covered(NIP_A, "Subject2", date(2026, 9, 20))
    assert led.is_covered(NIP_A, "Subject2", date(2026, 8, 20))
    led.close()


def test_poczatek_i_horyzont_pracy_starszej_wersji(tmp_path):
    """N3: moment najstarszego eksportu starszej wersji (per NIP) i pierwszy
    miesiąc objęty jej zakresami (per NIP × rodzaj) — zapamiętane przy
    migracji; świeża baza i firma bez wpisów — brak."""
    _stara_baza(tmp_path / "ledger.db", [
        (NIP_A, "1180012345-20260716-AAAA-01",
         r"C:\eks\a\hmffk_a_2026-07-01_2026-07-15_20260716-100000.TXT", "2026-07-16T08:00:00.123456+00:00"),
        (NIP_A, "1180012345-20260805-BBBB-02",
         r"C:\eks\a\hmffk_a_2026-07-01_2026-07-31_20260807-100000.TXT", "2026-08-07T08:00:00+00:00"),
        (NIP_A, f"{NIP_A}-20260820-CCCC-03",
         r"C:\eks\a\hmffk_a_2026-08-16_2026-08-31_20260901-100000.TXT", "2026-09-01T08:00:00+00:00"),
    ])
    led = Ledger(tmp_path / "ledger.db")
    assert led.legacy_start(NIP_A) == "2026-07-16T08:00:00.123456+00:00"
    assert led.legacy_start(NIP_B) is None
    assert led.legacy_horizon(NIP_A, "Subject2") == date(2026, 7, 1)
    assert led.legacy_horizon(NIP_A, "Subject1") == date(2026, 8, 1)
    # nowe zakresy (tej wersji) nie przesuwają horyzontu
    led.record_range(NIP_A, "Subject2", date(2026, 5, 1), date(2026, 5, 31))
    assert led.legacy_horizon(NIP_A, "Subject2") == date(2026, 7, 1)
    led.close()

    swieza = Ledger(tmp_path / "swieza.db")
    swieza.mark_done(NIP_A, "X", "hmffk_a_2026-07-01_2026-07-31_20260807-100000.TXT")
    assert swieza.legacy_start(NIP_A) is None  # wpis tej wersji, nie starszej
    assert swieza.legacy_horizon(NIP_A, "Subject2") is None
    swieza.close()
