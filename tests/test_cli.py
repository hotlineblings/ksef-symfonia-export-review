"""Testy CLI ograniczone do --sync-sql: ścieżka błędu musi przerywać eksport
(kod wyjścia 2) PRZED jakimkolwiek kontaktem z API KSeF — ochrona numeracji
kontrahentów. Pełny przebieg eksportu (KsefClient/run_export) nie jest tu
testowany — poza zakresem tego pliku."""

from app.cli import main
from app.kontrahenci_nr import RejestrNumerow
from app.symfonia_sql import SymfoniaSqlError


def test_sync_sql_blad_przerywa_eksport_kodem_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KSEF_TOKEN", "test-token")

    def fake_sync(self, profil_nip, baza):
        raise SymfoniaSqlError("brak sqlcmd w PATH")

    monkeypatch.setattr(RejestrNumerow, "synchronizuj_z_sql", fake_sync)

    kod = main([
        "--nip", "5252445719", "--cel", str(tmp_path), "--sync-sql", "TESTBAZA",
    ])

    assert kod == 2
    captured = capsys.readouterr()
    assert "TESTBAZA" in captured.err
    assert "brak sqlcmd w PATH" in captured.err


def test_sync_sql_zamyka_rejestr_nawet_przy_bledzie(tmp_path, monkeypatch):
    monkeypatch.setenv("KSEF_TOKEN", "test-token")
    zamkniety = []
    oryginalny_close = RejestrNumerow.close

    def sledzone_close(self):
        zamkniety.append(True)
        oryginalny_close(self)

    def fake_sync(self, profil_nip, baza):
        raise SymfoniaSqlError("timeout")

    monkeypatch.setattr(RejestrNumerow, "synchronizuj_z_sql", fake_sync)
    monkeypatch.setattr(RejestrNumerow, "close", sledzone_close)

    main(["--nip", "5252445719", "--cel", str(tmp_path), "--sync-sql", "TESTBAZA"])

    assert zamkniety == [True]
