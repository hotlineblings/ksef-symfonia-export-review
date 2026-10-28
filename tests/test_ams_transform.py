"""Golden testy: samples/*.xml -> nasz pipeline -> bajt-w-bajt == samples/*.TXT."""

from pathlib import Path

import pytest
from lxml import etree

from app.ams_transform import AmsTransformer, encode_cp1250, write_txt_atomic

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLES = PROJECT_ROOT / "samples"

GOLDEN_PAIRS = [
    ("ksefInvoice-koszt-EXTERNAL.xml", "ksefInvoice-koszt.TXT"),
    ("ksefInvoice-sprzedaz-INTERNAL.xml", "ksefInvoice-sprzedaz.TXT"),
]


@pytest.fixture(scope="module")
def transformer() -> AmsTransformer:
    return AmsTransformer()


@pytest.mark.parametrize("xml_name,txt_name", GOLDEN_PAIRS)
def test_golden_byte_for_byte(transformer, xml_name, txt_name):
    tree = etree.parse(str(SAMPLES / xml_name))
    actual = transformer.to_txt_bytes(tree)
    expected = (SAMPLES / txt_name).read_bytes()
    assert actual == expected


def test_golden_has_crlf_and_cp1250(transformer):
    tree = etree.parse(str(SAMPLES / GOLDEN_PAIRS[0][0]))
    data = transformer.to_txt_bytes(tree)
    text = data.decode("cp1250")
    assert "\r\n" in text
    assert "\n" not in text.replace("\r\n", "")
    # polskie znaki z nagłówka INFO przetrwały w CP1250
    assert "Księgowość".encode("cp1250") in data


def test_encode_cp1250_translit():
    data, bad = encode_cp1250("Łódź — test ß œ Ω")
    text = data.decode("cp1250")
    assert text.startswith("Łódź — test ß")  # wszystkie w CP1250
    assert "œ" not in text and "Ω" not in text
    assert "oe" in text  # NFKD/translit œ -> oe
    assert "?" in text  # Ω nie ma odpowiednika
    assert "œ" in bad and "Ω" in bad


def test_write_txt_atomic(tmp_path):
    target = tmp_path / "sub" / "out.TXT"
    write_txt_atomic(target, b"abc")
    assert target.read_bytes() == b"abc"
    assert not list(tmp_path.glob("**/*.tmp"))
