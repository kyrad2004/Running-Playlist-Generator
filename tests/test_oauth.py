from rpg.oauth import (
    CallbackResult,
    code_challenge_s256,
    generate_code_verifier,
    generate_state,
)


def test_pkce_challenge_matches_rfc7636_test_vector():
    """Appendix B of RFC 7636. If this drifts, Spotify rejects the token exchange."""
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert code_challenge_s256(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_challenge_is_unpadded_base64url():
    challenge = code_challenge_s256(generate_code_verifier())
    assert "=" not in challenge
    assert "+" not in challenge and "/" not in challenge


def test_verifier_length_within_spec():
    for _ in range(20):
        verifier = generate_code_verifier()
        assert 43 <= len(verifier) <= 128


def test_state_values_are_unique():
    assert len({generate_state() for _ in range(50)}) == 50


def test_callback_result_accessors():
    result = CallbackResult({"code": "abc", "state": "xyz"})
    assert result.code == "abc"
    assert result.state == "xyz"
    assert result.error is None

    denied = CallbackResult({"error": "access_denied"})
    assert denied.error == "access_denied"
    assert denied.code is None
