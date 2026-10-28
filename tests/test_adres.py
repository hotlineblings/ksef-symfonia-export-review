"""Testy parsera adresów FA(3) -> pola strukturalne karty kontrahenta."""

import pytest

from app.adres import parse_adres


@pytest.mark.parametrize(
    "l1,l2,expected",
    [
        (
            "ul. Przykladowa 12 lok. 3", "00-001 Warszawa",
            ("Przykladowa", "12", "3", "00-001", "Warszawa"),
        ),
        (
            "UL. DŁUGA 5", "00-950 KRAKÓW",
            ("DŁUGA", "5", "", "00-950", "KRAKÓW"),
        ),
        (
            "Aleje Jerozolimskie 99A/12", "02-001 Warszawa",
            ("Aleje Jerozolimskie", "99A", "12", "02-001", "Warszawa"),
        ),
        (   # wszystko w jednej linii
            "ul. Polna 7, 62-095 Murowana Goślina", "",
            ("Polna", "7", "", "62-095", "Murowana Goślina"),
        ),
        (   # miejscowość przed kodem
            "ul. Krótka 2", "Poznań, 61-001",
            ("Krótka", "2", "", "61-001", "Poznań"),
        ),
        (   # adres zagraniczny — bez rozbijania, nic nie ginie
            "Musterstrasse 5", "10115 Berlin",
            ("Musterstrasse 5", "", "", "", "10115 Berlin"),
        ),
        (   # dom z literą, "m." jako lokal
            "Wiejska 4B m. 7", "00-902 Warszawa",
            ("Wiejska", "4B", "7", "00-902", "Warszawa"),
        ),
        (   # brak numeru — cała linia jako ulica
            "Rynek Główny", "31-042 Kraków",
            ("Rynek Główny", "", "", "31-042", "Kraków"),
        ),
    ],
)
def test_parse_adres(l1, l2, expected):
    s = parse_adres(l1, l2)
    assert (s.ulica, s.nr_domu, s.nr_lokalu, s.kod_pocztowy, s.miejscowosc) == expected
