"""
Signed JaaS join links (SRS §11.3).

These verify the tokens the way JaaS verifies them — RS256 against the public
key, `aud="jitsi"`, `iss="chat"`, `nbf`/`exp` — using a key pair generated for
the test. They cannot show that JaaS accepts the tenant's real key or that media
flows: that needs a JaaS account and is recorded as not verified.
"""

from __future__ import annotations

import datetime as dt
from urllib.parse import parse_qs, urlparse

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.checks import run_checks
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.interviews import policies
from apps.interviews.checks import video_provider_is_real
from apps.interviews.models import InterviewParticipant, ParticipantRole, SessionState
from apps.interviews.providers import RoomProviderError
from apps.interviews.providers.jitsi import JitsiRoomProvider
from apps.interviews.services import rooms
from apps.interviews.tests.test_live import LiveFixture

APP_ID = "vpaas-magic-cookie-0123456789abcdef"
KID = f"{APP_ID}/key1"


def _pair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private, public


PRIVATE, PUBLIC = _pair()
OTHER_PRIVATE, _ = _pair()

JAAS = {
    "VIDEO_ROOM_PROVIDER": "apps.interviews.providers.jitsi.JitsiRoomProvider",
    "VIDEO_ROOM_OPTIONS": {"app_id": APP_ID, "api_key_id": KID, "private_key": PRIVATE},
}


def verify(token: str, **options):
    """What JaaS does at the door."""
    return jwt.decode(
        token, PUBLIC, algorithms=["RS256"], audience="jitsi", issuer="chat", **options
    )


class TokenTests(SimpleTestCase):
    def setUp(self):
        self.provider = JitsiRoomProvider(app_id=APP_ID, api_key_id=KID, private_key=PRIVATE)
        self.expires = timezone.now() + dt.timedelta(minutes=50)

    def token(self, **overrides):
        fields = {
            "room_ref": "careerai-room-a",
            "display_name": "Asha Rai",
            "is_host": False,
            "user_id": "careerai-abc",
            "expires_at": self.expires,
        }
        fields.update(overrides)
        return self.provider.token_for(**fields)

    def test_the_claims_are_the_ones_jaas_requires(self):
        claims = verify(self.token())

        self.assertEqual(claims["sub"], APP_ID)
        self.assertEqual(claims["room"], "careerai-room-a")
        self.assertEqual(claims["exp"], int(self.expires.timestamp()))
        self.assertLessEqual(claims["nbf"], int(timezone.now().timestamp()))
        self.assertEqual(claims["context"]["user"]["name"], "Asha Rai")
        self.assertEqual(claims["context"]["user"]["id"], "careerai-abc")

    def test_the_header_names_the_key(self):
        header = jwt.get_unverified_header(self.token())
        self.assertEqual(header["kid"], KID)
        self.assertEqual(header["alg"], "RS256")
        self.assertEqual(header["typ"], "JWT")

    def test_only_the_interviewer_is_a_moderator(self):
        self.assertEqual(verify(self.token(is_host=True))["context"]["user"]["moderator"], "true")
        self.assertEqual(verify(self.token(is_host=False))["context"]["user"]["moderator"], "false")

    def test_a_token_names_one_room_never_the_wildcard(self):
        claims = verify(self.token())
        self.assertNotEqual(claims["room"], "*")
        self.assertFalse(claims["context"]["room"]["regex"])

    def test_recording_and_streaming_are_switched_off(self):
        features = verify(self.token())["context"]["features"]
        self.assertFalse(features["recording"])
        self.assertFalse(features["livestreaming"])
        self.assertFalse(features["transcription"])

    def test_an_expired_token_is_refused(self):
        past = timezone.now() - dt.timedelta(hours=3)
        token = self.provider.token_for(
            room_ref="r",
            display_name="x",
            is_host=False,
            expires_at=past + dt.timedelta(minutes=5),
            now=past,
        )
        with self.assertRaises(jwt.ExpiredSignatureError):
            verify(token)

    def test_a_token_minted_for_later_is_not_yet_valid(self):
        later = timezone.now() + dt.timedelta(hours=2)
        token = self.provider.token_for(
            room_ref="r",
            display_name="x",
            is_host=False,
            expires_at=later + dt.timedelta(minutes=5),
            now=later,
        )
        with self.assertRaises(jwt.ImmatureSignatureError):
            verify(token)

    def test_an_altered_token_is_refused(self):
        header, payload, signature = self.token().split(".")
        forged = jwt.encode(
            {"room": "*", "aud": "jitsi", "iss": "chat"}, "x", algorithm="HS256"
        ).split(".")[1]
        with self.assertRaises(jwt.InvalidSignatureError):
            verify(f"{header}.{forged}.{signature}")

    def test_a_token_signed_with_another_key_is_refused(self):
        other = JitsiRoomProvider(app_id=APP_ID, api_key_id=KID, private_key=OTHER_PRIVATE)
        token = other.token_for(
            room_ref="r", display_name="x", is_host=True, expires_at=self.expires
        )
        with self.assertRaises(jwt.InvalidSignatureError):
            verify(token)

    def test_the_url_carries_the_token_and_the_app(self):
        url = self.provider.join_url_for(
            room_ref="careerai-room-a", display_name="Asha", is_host=False, expires_at=self.expires
        )
        parsed = urlparse(url)

        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}", "https://8x8.vc")
        self.assertEqual(parsed.path, f"/{APP_ID}/careerai-room-a")
        verify(parse_qs(parsed.query)["jwt"][0])

    def test_no_private_key_means_no_link_rather_than_an_unsigned_one(self):
        bare = JitsiRoomProvider(app_id=APP_ID, api_key_id=KID)
        with self.assertRaises(RoomProviderError):
            bare.join_url_for(room_ref="r", display_name="x", is_host=False)

    def test_no_app_id_means_no_link(self):
        with self.assertRaises(RoomProviderError):
            JitsiRoomProvider(private_key=PRIVATE).join_url_for(
                room_ref="r", display_name="x", is_host=False
            )

    def test_a_link_for_a_closed_room_is_refused(self):
        with self.assertRaises(RoomProviderError):
            self.token(expires_at=timezone.now() - dt.timedelta(minutes=1))

    def test_a_key_from_an_environment_variable_with_escaped_newlines_works(self):
        escaped = PRIVATE.replace("\n", "\\n")
        provider = JitsiRoomProvider(app_id=APP_ID, api_key_id=KID, private_key=escaped)
        verify(
            provider.token_for(
                room_ref="r", display_name="x", is_host=False, expires_at=self.expires
            )
        )

    def test_room_names_are_unguessable_and_say_nothing(self):
        first = self.provider.create_room(
            subject="Interview: Senior Backend", starts_at=None, ends_at=None
        )
        second = self.provider.create_room(
            subject="Interview: Senior Backend", starts_at=None, ends_at=None
        )

        self.assertNotEqual(first.room_ref, second.room_ref)
        self.assertGreaterEqual(len(first.room_ref), 30)
        self.assertNotIn("backend", first.room_ref.lower())


@override_settings(**JAAS)
class JoinViewTests(LiveFixture):
    def query_token(self, url: str) -> dict:
        return verify(parse_qs(urlparse(url).query)["jwt"][0])

    def test_the_interviewer_enters_as_moderator_until_the_window_closes(self):
        self.starts_in(5)
        self.client.force_login(self.owner)

        response = self.client.get(self.employer_url("interview_join"))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response["Location"].startswith(f"https://8x8.vc/{APP_ID}/{self.session.room_ref}?jwt=")
        )
        claims = self.query_token(response["Location"])
        self.assertEqual(claims["room"], self.session.room_ref)
        self.assertEqual(claims["context"]["user"]["moderator"], "true")
        self.session.refresh_from_db()
        self.assertEqual(claims["exp"], int(policies.join_window(self.session)[1].timestamp()))

    def test_the_candidate_enters_as_a_participant(self):
        self.starts_in(5)
        response = self.client.get(reverse("interviews:join", args=[self.session.public_id]))

        url = response.context["join_url"]
        claims = self.query_token(url)
        self.assertEqual(claims["context"]["user"]["moderator"], "false")
        self.assertEqual(claims["room"], self.session.room_ref)

    def test_the_identity_sent_to_the_provider_is_a_pseudonym(self):
        self.starts_in(5)
        response = self.client.get(reverse("interviews:join", args=[self.session.public_id]))
        user = self.query_token(response.context["join_url"])["context"]["user"]

        self.assertEqual(user["id"], rooms.pseudonym(self.profile.user))
        self.assertNotIn(self.profile.user.email, str(user))
        self.assertEqual(user["email"], "")

    def test_a_company_member_not_on_the_panel_cannot_enter(self):
        from apps.candidates.tests.factories import create_profile
        from apps.companies.tests.factories import add_member
        from apps.jobs.models import JobAssignment

        self.starts_in(5)
        colleague = create_profile().user
        # Assigned to the job, so the interview is readable (decision D-13) and
        # what is under test is the panel rule, not the applicant scope.
        membership = add_member(self.company, colleague)
        JobAssignment.objects.create(job=self.session.application.job, membership=membership)
        self.client.force_login(colleague)

        response = self.client.get(self.employer_url("interview_join"), follow=True)
        # The page escapes the apostrophe, so the message is read from the context.
        self.assertIn(
            "not on this interview's panel",
            " ".join(str(message) for message in response.context["messages"]),
        )
        self.session.refresh_from_db()
        self.assertEqual(self.session.state, SessionState.SCHEDULED)

    def test_an_observer_enters_without_moderator_rights(self):
        from apps.candidates.tests.factories import create_profile
        from apps.companies.permissions import Role
        from apps.companies.tests.factories import add_member

        self.starts_in(5)
        observer = create_profile().user
        # An Interviewer reads through the panel seat; an unassigned Recruiter
        # would not reach the interview at all (decision D-13).
        add_member(self.company, observer, role=Role.INTERVIEWER)
        InterviewParticipant.objects.create(
            session=self.session, user=observer, role=ParticipantRole.OBSERVER
        )
        self.client.force_login(observer)

        response = self.client.get(self.employer_url("interview_join"))

        self.assertEqual(
            self.query_token(response["Location"])["context"]["user"]["moderator"], "false"
        )

    def test_a_closed_room_mints_no_token(self):
        self.client.force_login(self.owner)
        response = self.client.get(self.employer_url("interview_join"), follow=True)

        self.assertContains(response, "opens fifteen minutes before")
        self.assertNotIn("jwt=", response.redirect_chain[-1][0])

    @override_settings(
        VIDEO_ROOM_PROVIDER="apps.interviews.providers.jitsi.JitsiRoomProvider",
        VIDEO_ROOM_OPTIONS={"app_id": APP_ID, "api_key_id": KID},
    )
    def test_a_missing_key_is_a_message_not_a_broken_link(self):
        self.starts_in(5)
        response = self.client.get(reverse("interviews:join", args=[self.session.public_id]))

        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "could not be opened", status_code=409)
        self.assertNotContains(response, "8x8.vc", status_code=409)


class DeployCheckTests(SimpleTestCase):
    @override_settings(
        DEBUG=False, VIDEO_ROOM_PROVIDER="apps.interviews.providers.fake.FakeRoomProvider"
    )
    def test_production_refuses_the_fake_provider(self):
        self.assertEqual([e.id for e in video_provider_is_real()], ["interviews.E001"])

    @override_settings(
        DEBUG=False,
        VIDEO_ROOM_PROVIDER=JAAS["VIDEO_ROOM_PROVIDER"],
        VIDEO_ROOM_OPTIONS={"app_id": APP_ID},
    )
    def test_production_refuses_an_incomplete_jaas_setup(self):
        errors = video_provider_is_real()
        self.assertEqual([e.id for e in errors], ["interviews.E002"])
        self.assertIn("JAAS_API_KEY_ID", errors[0].msg)

    @override_settings(DEBUG=False, **JAAS)
    def test_a_configured_jaas_setup_passes(self):
        self.assertEqual(video_provider_is_real(), [])

    @override_settings(
        DEBUG=True, VIDEO_ROOM_PROVIDER="apps.interviews.providers.fake.FakeRoomProvider"
    )
    def test_development_may_use_the_fake(self):
        self.assertEqual(video_provider_is_real(), [])

    def test_the_check_is_registered_for_deploy(self):
        with override_settings(
            DEBUG=False, VIDEO_ROOM_PROVIDER="apps.interviews.providers.fake.FakeRoomProvider"
        ):
            ids = [message.id for message in run_checks(include_deployment_checks=True)]
        self.assertIn("interviews.E001", ids)
