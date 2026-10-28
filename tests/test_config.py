"""Testy zaszyfrowanej konfiguracji."""

import base64
import json

import pytest

from app.config import AppConfig, ConfigStore, Profile, WrongMasterPassword, ConfigCorrupted


def test_roundtrip(tmp_path):
    store = ConfigStore(tmp_path / "config.enc")
    assert not store.exists()
    cfg = store.initialize("Bardzo tajne haslo 123")
    cfg.profiles.append(Profile("Firma A", "5252445719", "sekretny-token", "firma-a"))
    cfg.target_dir = r"C:\eksport"
    store.save(cfg)

    store2 = ConfigStore(tmp_path / "config.enc")
    assert store2.exists()
    loaded = store2.unlock("Bardzo tajne haslo 123")
    assert loaded.profiles[0].nip == "5252445719"
    assert loaded.profiles[0].token == "sekretny-token"
    assert loaded.target_dir == r"C:\eksport"


def test_wrong_password(tmp_path):
    store = ConfigStore(tmp_path / "config.enc")
    store.initialize("dobre-haslo")
    with pytest.raises(WrongMasterPassword):
        ConfigStore(tmp_path / "config.enc").unlock("zle-haslo")


def test_token_not_plaintext_on_disk(tmp_path):
    store = ConfigStore(tmp_path / "config.enc")
    cfg = store.initialize("haslo")
    cfg.profiles.append(Profile("F", "1234567890", "super-tajny-token-ksef", "f"))
    store.save(cfg)
    raw = (tmp_path / "config.enc").read_bytes()
    assert b"super-tajny-token-ksef" not in raw


def test_corrupted_file(tmp_path):
    path = tmp_path / "config.enc"
    path.write_text(json.dumps({"version": 1, "salt": "!!!", "payload": "xxx"}))
    with pytest.raises((ConfigCorrupted, WrongMasterPassword)):
        ConfigStore(path).unlock("haslo")


def test_masked_token():
    p = Profile("F", "123", "abcdefghij", "f")
    assert p.masked_token() == "abcdef…"
    assert "abcdefghij" not in p.masked_token()


def test_konto_prefix_roundtrip(tmp_path):
    store = ConfigStore(tmp_path / "config.enc")
    cfg = store.initialize("haslo-konto")
    # domyślne prefiksy/konta od razu po initialize()
    assert cfg.konto_prefix_sprzedaz == "201-2"
    assert cfg.konto_prefix_koszt == "202-2"
    assert cfg.konto_sprzedaz_netto == "703"
    assert cfg.konto_sprzedaz_vat == "222-1"
    cfg.konto_prefix_sprzedaz = "201-9"
    cfg.konto_prefix_koszt = "202-9"
    cfg.konto_sprzedaz_netto = "700"
    cfg.konto_sprzedaz_vat = "222-9"
    store.save(cfg)

    loaded = ConfigStore(tmp_path / "config.enc").unlock("haslo-konto")
    assert loaded.konto_prefix_sprzedaz == "201-9"
    assert loaded.konto_prefix_koszt == "202-9"
    assert loaded.konto_sprzedaz_netto == "700"
    assert loaded.konto_sprzedaz_vat == "222-9"


def test_konto_koszt_i_termin_fallback_roundtrip(tmp_path):
    store = ConfigStore(tmp_path / "config.enc")
    cfg = store.initialize("haslo-koszt")
    # domyślne wartości od razu po initialize()
    assert cfg.konto_koszt_vat == "221-1"
    assert cfg.konto_koszt_netto == ""
    assert cfg.termin_fallback_dni == 14
    cfg.konto_koszt_vat = "221-9"
    cfg.konto_koszt_netto = "401"
    cfg.termin_fallback_dni = 30
    store.save(cfg)

    loaded = ConfigStore(tmp_path / "config.enc").unlock("haslo-koszt")
    assert loaded.konto_koszt_vat == "221-9"
    assert loaded.konto_koszt_netto == "401"
    assert loaded.termin_fallback_dni == 30
    assert isinstance(loaded.termin_fallback_dni, int)


def test_konto_prefix_default_dla_starego_configu(tmp_path):
    """config.enc sprzed dodania pól konto_prefix_* -> unlock() ma podstawić defaulty.
    Stary klucz "konto_prefix" (sprzed rozdzielenia na dwa prefiksy) jest ignorowany —
    funkcja nie była jeszcze wydana, więc wsteczna zgodność z nim nie jest wymagana."""
    path = tmp_path / "config.enc"
    store = ConfigStore(path)
    store.initialize("haslo")  # ustawia sól/fernet w store, żeby zaszyfrować payload ręcznie
    stary_payload = json.dumps(
        {
            "profiles": [], "target_dir": r"C:\eksport", "ksef_base_url": "https://x",
            "konto_prefix": "999-9",  # stary klucz sprzed tej funkcji — celowo nieobsługiwany
        }
    )
    payload = store._fernet.encrypt(stary_payload.encode("utf-8"))
    envelope = {
        "version": 1,
        "kdf": "scrypt",
        "salt": base64.b64encode(store._salt).decode("ascii"),
        "n": 2**15,
        "r": 8,
        "p": 1,
        "payload": payload.decode("ascii"),
    }
    path.write_text(json.dumps(envelope), encoding="utf-8")

    loaded = ConfigStore(path).unlock("haslo")
    assert loaded.konto_prefix_sprzedaz == "201-2"
    assert loaded.konto_prefix_koszt == "202-2"
    assert loaded.konto_sprzedaz_netto == "703"
    assert loaded.konto_sprzedaz_vat == "222-1"
    # pola dodane później (konto kosztu, termin fallback) — też muszą dostać defaulty
    assert loaded.konto_koszt_vat == "221-1"
    assert loaded.konto_koszt_netto == ""
    assert loaded.termin_fallback_dni == 14
    assert loaded.target_dir == r"C:\eksport"


def test_profil_konta_override_roundtrip(tmp_path):
    """Firma typu BETALABO księguje przychody na 701 zamiast wspólnego 703 —
    nadpisanie trzyma się profilu i przeżywa save/unlock."""
    store = ConfigStore(tmp_path / "config.enc")
    cfg = store.initialize("haslo-override")
    profil = Profile(
        "BETALABO", "1112223344", "token-aqt", "betalabo",
        konta_override={"konto_sprzedaz_netto": "701"},
    )
    cfg.profiles.append(profil)
    store.save(cfg)

    loaded = ConfigStore(tmp_path / "config.enc").unlock("haslo-override")
    assert loaded.profiles[0].konta_override == {"konto_sprzedaz_netto": "701"}


def test_profil_bez_konta_override_stary_payload(tmp_path):
    """Stary profil zapisany przed dodaniem konta_override -> unlock() ma
    podstawić pusty dict, nie wywalić się na brakującym kluczu."""
    path = tmp_path / "config.enc"
    store = ConfigStore(path)
    store.initialize("haslo")
    stary_payload = json.dumps(
        {
            "profiles": [
                {"name": "Stara Firma", "nip": "9998887766", "token": "t", "subfolder": "sf"}
            ],
            "target_dir": "", "ksef_base_url": "https://x",
        }
    )
    payload = store._fernet.encrypt(stary_payload.encode("utf-8"))
    envelope = {
        "version": 1, "kdf": "scrypt",
        "salt": base64.b64encode(store._salt).decode("ascii"),
        "n": 2**15, "r": 8, "p": 1,
        "payload": payload.decode("ascii"),
    }
    path.write_text(json.dumps(envelope), encoding="utf-8")

    loaded = ConfigStore(path).unlock("haslo")
    assert loaded.profiles[0].konta_override == {}


def test_konta_dla_nadpisanie_i_globalne(tmp_path):
    store = ConfigStore(tmp_path / "config.enc")
    cfg = store.initialize("haslo-konta-dla")
    profil_z_nadpisaniem = Profile(
        "BETALABO", "1112223344", "t", "aqt",
        konta_override={"konto_sprzedaz_netto": "701"},
    )
    profil_bez_nadpisania = Profile("Inna Firma", "5556667778", "t", "inna")

    efektywne_a = cfg.konta_dla(profil_z_nadpisaniem)
    assert efektywne_a["konto_sprzedaz_netto"] == "701"
    # pola bez nadpisania nadal biorą wartość globalną
    assert efektywne_a["konto_prefix_sprzedaz"] == cfg.konto_prefix_sprzedaz
    assert efektywne_a["termin_fallback_dni"] == cfg.termin_fallback_dni

    efektywne_b = cfg.konta_dla(profil_bez_nadpisania)
    assert efektywne_b["konto_sprzedaz_netto"] == cfg.konto_sprzedaz_netto


def test_konta_dla_termin_fallback_dni_override(tmp_path):
    store = ConfigStore(tmp_path / "config.enc")
    cfg = store.initialize("haslo-termin")
    profil = Profile("Firma", "1231231231", "t", "f", konta_override={"termin_fallback_dni": "21"})
    efektywne = cfg.konta_dla(profil)
    assert efektywne["termin_fallback_dni"] == 21
    assert isinstance(efektywne["termin_fallback_dni"], int)


def test_profil_db_symfonia_i_sql_autosync_roundtrip(tmp_path):
    """Mapowanie bazy Symfonii i flaga auto-sync trzymają się profilu i
    przeżywają save/unlock."""
    store = ConfigStore(tmp_path / "config.enc")
    cfg = store.initialize("haslo-sql")
    profil = Profile(
        "ALFATRON_SERWIS", "1112223344", "token", "alfatron-serwis",
        db_symfonia="ALFATRON_SERWIS_MEDYCZNY", sql_autosync=False,
    )
    cfg.profiles.append(profil)
    store.save(cfg)

    loaded = ConfigStore(tmp_path / "config.enc").unlock("haslo-sql")
    assert loaded.profiles[0].db_symfonia == "ALFATRON_SERWIS_MEDYCZNY"
    assert loaded.profiles[0].sql_autosync is False


def test_profil_db_symfonia_default_dla_starego_profilu(tmp_path):
    """Profil zapisany przed dodaniem db_symfonia/sql_autosync -> unlock() ma
    podstawić defaulty ("" i True), nie wywalić się na brakującym kluczu."""
    path = tmp_path / "config.enc"
    store = ConfigStore(path)
    store.initialize("haslo")
    stary_payload = json.dumps(
        {
            "profiles": [
                {"name": "Stara Firma", "nip": "9998887766", "token": "t", "subfolder": "sf"}
            ],
            "target_dir": "", "ksef_base_url": "https://x",
        }
    )
    payload = store._fernet.encrypt(stary_payload.encode("utf-8"))
    envelope = {
        "version": 1, "kdf": "scrypt",
        "salt": base64.b64encode(store._salt).decode("ascii"),
        "n": 2**15, "r": 8, "p": 1,
        "payload": payload.decode("ascii"),
    }
    path.write_text(json.dumps(envelope), encoding="utf-8")

    loaded = ConfigStore(path).unlock("haslo")
    assert loaded.profiles[0].db_symfonia == ""
    assert loaded.profiles[0].sql_autosync is True


def test_profil_ostatnie_daty_roundtrip(tmp_path):
    """Zapamiętany zakres eksportu (per firma) trzyma się profilu i przeżywa
    save/unlock — patrz app/gui.py::_zakres_dla_profilu."""
    store = ConfigStore(tmp_path / "config.enc")
    cfg = store.initialize("haslo-daty")
    profil = Profile(
        "Firma", "1112223344", "token", "firma",
        ostatnie_od="2026-01-01", ostatnie_do="2026-01-31",
    )
    cfg.profiles.append(profil)
    store.save(cfg)

    loaded = ConfigStore(tmp_path / "config.enc").unlock("haslo-daty")
    assert loaded.profiles[0].ostatnie_od == "2026-01-01"
    assert loaded.profiles[0].ostatnie_do == "2026-01-31"


def test_profil_ostatnie_daty_default_dla_starego_profilu(tmp_path):
    """Profil zapisany przed dodaniem ostatnie_od/ostatnie_do -> unlock() ma
    podstawić puste stringi, nie wywalić się na brakującym kluczu."""
    path = tmp_path / "config.enc"
    store = ConfigStore(path)
    store.initialize("haslo")
    stary_payload = json.dumps(
        {
            "profiles": [
                {"name": "Stara Firma", "nip": "9998887766", "token": "t", "subfolder": "sf"}
            ],
            "target_dir": "", "ksef_base_url": "https://x",
        }
    )
    payload = store._fernet.encrypt(stary_payload.encode("utf-8"))
    envelope = {
        "version": 1, "kdf": "scrypt",
        "salt": base64.b64encode(store._salt).decode("ascii"),
        "n": 2**15, "r": 8, "p": 1,
        "payload": payload.decode("ascii"),
    }
    path.write_text(json.dumps(envelope), encoding="utf-8")

    loaded = ConfigStore(path).unlock("haslo")
    assert loaded.profiles[0].ostatnie_od == ""
    assert loaded.profiles[0].ostatnie_do == ""


def test_konta_dla_termin_fallback_dni_smieciowa_wartosc(tmp_path):
    """Śmieciowa wartość override (np. ręcznie uszkodzony config) -> wraca do
    wartości globalnej zamiast wywalać się wyjątkiem."""
    store = ConfigStore(tmp_path / "config.enc")
    cfg = store.initialize("haslo-smiec")
    profil = Profile("Firma", "1231231231", "t", "f", konta_override={"termin_fallback_dni": "abc"})
    efektywne = cfg.konta_dla(profil)
    assert efektywne["termin_fallback_dni"] == cfg.termin_fallback_dni
