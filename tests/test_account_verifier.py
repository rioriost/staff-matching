import base64
import json

import pytest
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.exceptions import InvalidAuthenticationResponse

from backend.poc_accounts_verify import TestAuthenticator as SoftwareAuthenticator


def test_private_network_authenticator_produces_real_verifiable_signatures():
    origin = "https://staff.example"
    authenticator = SoftwareAuthenticator("staff.example", origin)
    registration = generate_registration_options(
        rp_id="staff.example", rp_name="Staff verification",
        user_id=b"synthetic-user", user_name="verification",
    )
    result = verify_registration_response(
        credential=authenticator.registration(json.loads(options_to_json(registration))),
        expected_challenge=registration.challenge,
        expected_rp_id="staff.example", expected_origin=origin,
        require_user_verification=True,
    )
    assert result.credential_id == authenticator.credential_id
    assert result.user_verified
    options = generate_authentication_options(rp_id="staff.example")
    assertion = authenticator.assertion(json.loads(options_to_json(options)))
    verified = verify_authentication_response(
        credential=assertion,
        expected_challenge=options.challenge,
        expected_rp_id="staff.example", expected_origin=origin,
        credential_public_key=result.credential_public_key,
        credential_current_sign_count=result.sign_count,
        require_user_verification=True,
    )
    assert verified.new_sign_count == 1
    assert verified.user_verified
    signature = assertion["response"]["signature"]
    decoded = bytearray(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)))
    decoded[-1] ^= 1
    assertion["response"]["signature"] = base64.urlsafe_b64encode(decoded).decode().rstrip("=")
    with pytest.raises(InvalidAuthenticationResponse):
        verify_authentication_response(
            credential=assertion,
            expected_challenge=options.challenge,
            expected_rp_id="staff.example", expected_origin=origin,
            credential_public_key=result.credential_public_key,
            credential_current_sign_count=result.sign_count,
            require_user_verification=True,
        )
