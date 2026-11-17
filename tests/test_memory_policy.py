"""Política pura da memória automática."""
from __future__ import annotations

import pytest

from nemec import memory_policy as p


@pytest.mark.parametrize("text", ["Minha senha é 123", "password: x", "use o token abc", "API key da OpenAI", "CPF 123.456.789-09",
                                  "cartão 4111111111111111", "-----BEGIN RSA PRIVATE KEY-----", "A" * 40])
def test_secret_detected(text):
    assert p.secret_reason(text)


@pytest.mark.parametrize("text", ["O nome do usuário é Lucas", "Gosta de jazz e café", "Trabalha com Python desde 2019", "Mora em Recife"])
def test_ordinary_facts_are_not_secrets(text):
    assert p.secret_reason(text) is None


def test_normalize_ignores_case_accents_punctuation():
    assert p.normalize("  Café,  SEM açúcar! ") == "cafe sem acucar"


def test_literal_span_returns_exact_text_of_current_message():
    msg = "Olá! Meu nome é Ana, e eu moro em Recife."
    assert p.literal_span("meu nome é ana", msg) == "Meu nome é Ana"  # sem diferenciar caixa/pontuação; devolve o original
    assert p.literal_span("eu moro em Recife.", msg) == "eu moro em Recife"
    assert p.literal_span("Prefere cinema monocromático", "Como se escreve cinema?") is None  # paráfrase
    assert p.literal_span("O nome do usuário é Ana", msg) is None  # reescrita em 3ª pessoa
    assert p.literal_span("", msg) is None and p.literal_span("ok", "ok") is None


def test_find_duplicate_identical_similar_and_different():
    mems = [{"id": 1, "content": "Prefere café sem açúcar"}, {"id": 2, "content": "Mora em Recife"}]
    assert p.find_duplicate("prefere cafe sem acucar.", mems) == (mems[0], True)
    assert p.find_duplicate("Prefere café sem açúcar e leite", mems)[0] is mems[0]
    assert p.find_duplicate("Gosta de jazz", mems) == (None, False)
