"""Маскирование секретов в тексте с экрана (app/redact.py)."""
from app.redact import redact


def test_env_file_from_screen_is_masked_but_readable():
    text = "\n".join([
        "DATABASE_URL=postgres://user:S3cretPass@neon.tech/db",
        "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz0123456789",
        "GOOGLE_KEY=AIzaSyD-1234567890abcdefghijklmnopqrstuvw",
        'BETTER_AUTH_SECRET: "9f8e7d6c5b4a39281706f5e4d3c2b1a0ffeeddccbbaa99887766554433221100"',
        "TG=1234567890:AAHf3kL9mN0pQrStUvWxYz1234567890abcdefg",
        "JWT eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    ])
    out, n = redact(text)
    assert n >= 6
    for secret in ("S3cretPass", "sk-proj-abcdefghijklmnop", "AIzaSyD-1234567890",
                   "9f8e7d6c5b4a3928", "AAHf3kL9mN0pQrStUvWxYz", "eyJhbGciOiJIUzI1NiJ9"):
        assert secret not in out
    # Имена переменных остаются — видно, ЧТО показывали.
    assert "DATABASE_URL=postgres://user:[скрыто]@neon.tech/db" in out
    assert "OPENAI_API_KEY=[скрыто]" in out
    assert "GOOGLE_KEY=[скрыто" in out


def test_ordinary_meeting_text_is_untouched():
    text = ("Обсудили редизайн карточки товара, срок — 15 сентября, "
            "ссылка https://telemost.yandex.ru/j/1234567890123456 и "
            "тренажёр «Порядок счёта до 31». Password policy обсуждали устно.")
    out, n = redact(text)
    assert n == 0 and out == text


def test_private_key_block_is_replaced():
    pem = ("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC\n"
           "-----END PRIVATE KEY-----")
    out, n = redact("ключ:\n" + pem + "\nконец")
    assert n == 1 and "MIIEvQIBADAN" not in out and "[скрыто: приватный ключ]" in out
