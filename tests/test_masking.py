"""Masking: every secret format becomes a typed placeholder; ordinary text is untouched."""

from __future__ import annotations

import time

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sandcage.masking import (
    JsonValue,
    count_placeholders,
    is_env_file,
    is_secret_name,
    mask,
    mask_value,
    placeholder,
    secret_values,
)
from tests.secrets_fixtures import (
    ALNUM,
    JWT,
    PRIVATE_KEY_BLOCK,
    STRIPE_KEY,
    TOKEN_SAMPLES,
    UPPER_DIGITS,
)

# --- token formats -------------------------------------------------------------------


@pytest.mark.parametrize(("kind", "secret"), sorted(TOKEN_SAMPLES.items()))
def test_each_token_format_is_masked_with_its_kind(kind: str, secret: str) -> None:
    text = f"config: {secret} (copied from the dashboard)"
    masked = mask(text)
    assert secret not in masked
    assert placeholder(kind) in masked
    assert masked == f"config: {placeholder(kind)} (copied from the dashboard)"


def test_anthropic_key_is_not_reported_as_openai() -> None:
    assert mask(TOKEN_SAMPLES["anthropic_key"]) == placeholder("anthropic_key")


def test_demo_stripe_style_key_is_masked() -> None:
    assert mask(f"key {STRIPE_KEY}") == f"key {placeholder('stripe_key')}"


def test_bearer_token_keeps_the_word_and_masks_the_token() -> None:
    masked = mask("Authorization: Bearer abcDEF123456.xyz-_~+/=")
    assert masked == f"Authorization: Bearer {placeholder('bearer_token')}"


def test_bearer_with_known_format_is_masked_once() -> None:
    masked = mask(f"Authorization: Bearer {JWT}")
    assert masked == f"Authorization: Bearer {placeholder('jwt')}"


def test_email_addresses_are_masked() -> None:
    assert mask("contact ops.team+alerts@example.co.uk now") == (
        f"contact {placeholder('email')} now"
    )


# --- private keys --------------------------------------------------------------------


def test_private_key_block_is_masked_whole() -> None:
    text = f"before\n{PRIVATE_KEY_BLOCK}\nafter"
    assert mask(text) == f"before\n{placeholder('private_key')}\nafter"


def test_unterminated_private_key_masks_to_the_end() -> None:
    truncated = PRIVATE_KEY_BLOCK.rsplit("-----END", 1)[0]
    text = f"head\n{truncated}more secret material\nand more"
    assert mask(text) == f"head\n{placeholder('private_key')}"


@pytest.mark.parametrize("label", ["OPENSSH ", "EC ", "ENCRYPTED ", ""])
def test_private_key_block_variants(label: str) -> None:
    block = f"-----BEGIN {label}PRIVATE KEY-----\nAAAA\n-----END {label}PRIVATE KEY-----"
    assert mask(block) == placeholder("private_key")


# --- env files -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (".env", True),
        (".env.local", True),
        ("config/.env.production", True),
        ("prod.env", True),
        (".ENV", True),
        ("environment.py", False),
        ("env.py", False),
        ("README.md", False),
    ],
)
def test_is_env_file(name: str, expected: bool) -> None:
    assert is_env_file(name) is expected


def test_env_file_masks_every_value() -> None:
    text = "# comment stays\nDEBUG=true\nexport PORT = 8080\nNAME=invoice-calc\nEMPTY=\n"
    masked = mask(text, source="/workspace/.env")
    assert masked == (
        "# comment stays\n"
        f"DEBUG={placeholder('env_value')}\n"
        f"export PORT = {placeholder('env_value')}\n"
        f"NAME={placeholder('env_value')}\n"
        "EMPTY=\n"
    )


def test_env_values_are_not_masked_without_an_env_source() -> None:
    assert mask("DEBUG=true\nPORT=8080") == "DEBUG=true\nPORT=8080"


# --- secret-named assignments --------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("DB_PASSWORD", True),
        ("apiKey", True),
        ("api_key", True),
        ("API-KEY", True),
        ("client_secret", True),
        ("GITHUB_TOKEN", True),
        ("aws_secret_access_key", True),
        ("PRIVATE_KEY", True),
        ("auth", True),
        ("author", False),
        ("tokenizer", False),
        ("max_tokens", False),
        ("key", False),
        ("passenger", False),
    ],
)
def test_is_secret_name(name: str, expected: bool) -> None:
    assert is_secret_name(name) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('password = "hunter22"', f'password = "{placeholder("secret_value")}"'),
        ("db_password='s3cr3t!'", f"db_password='{placeholder('secret_value')}'"),
        ('{"api_key": "abcd1234"}', f'{{"api_key": "{placeholder("secret_value")}"}}'),
        ('connect(token="zzzzzzzz")', f'connect(token="{placeholder("secret_value")}")'),
        ('if secret == "letmein": pass', f'if secret == "{placeholder("secret_value")}": pass'),
    ],
)
def test_quoted_secret_assignments_are_masked_anywhere(text: str, expected: str) -> None:
    assert mask(text) == expected
    assert mask(text, source="app/settings.py") == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("password: hunter22", f"password: {placeholder('secret_value')}"),
        (
            "DB_PASSWORD=correct-horse  # prod",
            f"DB_PASSWORD={placeholder('secret_value')}  # prod",
        ),
        ("  client_secret = abc123xyz", f"  client_secret = {placeholder('secret_value')}"),
    ],
)
def test_unquoted_secret_assignments_are_masked_outside_code(text: str, expected: str) -> None:
    assert mask(text) == expected
    assert mask(text, source="config.yaml") == expected


@pytest.mark.parametrize(
    "line",
    [
        "token = os.environ.get('TOKEN')",
        "password = settings.DB_PASSWORD",
        "auth = None",
        "api_key: ${API_KEY}",
        "MAX_TOKENS=3000",
        'password = ""',
        "token = get_token()",
    ],
)
def test_references_and_expressions_are_not_masked(line: str) -> None:
    assert mask(line) == line


def test_unquoted_values_in_source_code_are_left_alone() -> None:
    line = "password = default_password"
    assert mask(line, source="app/auth.py") == line


# --- ordinary text -------------------------------------------------------------------

ORDINARY_CODE = '''\
"""Invoice helpers."""

from decimal import Decimal


def apply_discount(amount: Decimal, percent: Decimal) -> Decimal:
    """Take percent off the amount: 200 with 10% off is 180."""
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    return amount - amount * percent / 100


class Tokenizer:
    def __init__(self, max_tokens: int = 3000) -> None:
        self.max_tokens = max_tokens
        self.author = "invoice team"

    def split(self, text: str) -> list[str]:
        return text.split()

matrix = a @ b
risk_assessment_for_the_new_module_v2 = "draft"
'''


def test_ordinary_code_is_untouched() -> None:
    assert mask(ORDINARY_CODE) == ORDINARY_CODE
    assert mask(ORDINARY_CODE, source="invoice_calc/invoice.py") == ORDINARY_CODE


def test_ordinary_prose_with_sk_words_is_untouched() -> None:
    text = "We discussed the risk-assessment-for-the-new-module and task-antelope-migration."
    assert mask(text) == text


# --- nested data ---------------------------------------------------------------------


def test_mask_value_recurses_through_nested_data() -> None:
    secret = TOKEN_SAMPLES["github_token"]
    data: JsonValue = {
        "tool": "run_command",
        "args": {"command": f"echo {secret}", "env": [f"x {secret}", 3, None, True]},
        secret: "key position",
    }
    masked = mask_value(data)
    assert secret not in repr(masked)
    assert masked == {
        "tool": "run_command",
        "args": {
            "command": f"echo {placeholder('github_token')}",
            "env": [f"x {placeholder('github_token')}", 3, None, True],
        },
        placeholder("github_token"): "key position",
    }


def test_count_placeholders() -> None:
    assert count_placeholders(f"x = {placeholder('jwt')} {placeholder('email')}") == 2
    assert count_placeholders("x = [MASKED]") == 0


# --- properties ----------------------------------------------------------------------

_SEPARATORS = st.sampled_from([" ", "\n", "\t", ", ", "; ", "(", ")", '"', "'", ": ", "= "])
_FILLER = st.text(alphabet=ALNUM + " .,;:!?()-_\n", max_size=40)


def _token(prefix: str, size: int, alphabet: str = ALNUM) -> st.SearchStrategy[str]:
    return st.text(alphabet=alphabet, min_size=size, max_size=size + 10).map(
        lambda body: prefix + body
    )


_SECRETS = st.one_of(
    _token("sk-" + "ant-", 24),
    _token("sk-" + "proj-", 24).map(lambda s: s + "9"),
    _token("sk_" + "live_", 12),
    _token("gsk" + "_", 24),
    _token("AK" + "IA", 16, UPPER_DIGITS),
    _token("gh" + "p_", 36),
    _token("github_" + "pat_", 24),
    _token("xox" + "b-", 12),
    _token("AI" + "za", 35).map(lambda s: s[:39]),
    _token("hf" + "_", 32),
)


@given(before=_FILLER, sep1=_SEPARATORS, secret=_SECRETS, sep2=_SEPARATORS, after=_FILLER)
def test_no_token_survives_masking(
    before: str, sep1: str, secret: str, sep2: str, after: str
) -> None:
    masked = mask(before + sep1 + secret + sep2 + after)
    assert secret not in masked


@given(
    name=st.sampled_from(["password", "DB_PASSWORD", "apiKey", "client_secret", "auth_token"]),
    value=st.text(alphabet=ALNUM + "!#%&*+-./?@^_~", min_size=4, max_size=30),
    quote=st.sampled_from(['"', "'"]),
    sep=st.sampled_from([" = ", "=", ": ", ":"]),
)
def test_no_quoted_secret_assignment_survives(name: str, value: str, quote: str, sep: str) -> None:
    masked = mask(f"{name}{sep}{quote}{value}{quote}")
    assert masked == f"{name}{sep}{quote}{placeholder('secret_value')}{quote}"


@given(body=st.text(alphabet=ALNUM + "+/=\n", max_size=200), terminated=st.booleans())
def test_no_private_key_body_survives(body: str, terminated: bool) -> None:
    secret_body = "MII" + body + "QQ"
    end = "\n-----END " + "PRIVATE KEY-----" if terminated else ""
    text = "-----BEGIN " + "PRIVATE KEY-----\n" + secret_body + end
    assert secret_body not in mask(text)


@given(
    lines=st.lists(
        st.tuples(
            st.from_regex(r"[A-Z_][A-Z0-9_]{0,10}", fullmatch=True),
            st.text(alphabet=ALNUM + "!@#%^&*()-+=/.,", min_size=1, max_size=20),
        ),
        max_size=8,
    )
)
def test_no_env_value_survives(lines: list[tuple[str, str]]) -> None:
    text = "\n".join(f"{name}={value}" for name, value in lines)
    masked = mask(text, source=".env")
    for masked_line, (name, _value) in zip(masked.split("\n"), lines, strict=False):
        assert masked_line == f"{name}={placeholder('env_value')}"


@given(st.text(max_size=300))
def test_mask_is_idempotent_and_total(text: str) -> None:
    once = mask(text)
    assert mask(once) == once
    env_once = mask(text, source=".env")
    assert mask(env_once, source=".env") == env_once


# --- cost ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["a" * 200_000, "a." * 100_000, '"a' * 100_000, "x-y_z." * 33_000],
    ids=["word", "dotted", "quotes", "mixed"],
)
def test_masking_is_linear_on_long_unbroken_input(text: str) -> None:
    # A planted minified file is one long line. Quadratic matching took about 40 s for
    # 50 KB; linear matching takes milliseconds, so this bound cannot flake.
    start = time.perf_counter()
    mask(text)
    secret_values(text)
    assert time.perf_counter() - start < 2.0
