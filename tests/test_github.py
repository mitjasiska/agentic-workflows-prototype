import io
import json
import os
import socket
import ssl
import traceback
import unittest
from http.client import IncompleteRead, InvalidURL, RemoteDisconnected
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from task_start import TaskError
from task_start.github import api_credential, request


SECRET = "test-secret-must-not-appear"


class GitHubRequestTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"GH_TOKEN": SECRET, "GITHUB_TOKEN": ""}))
        self.cli = self.enterContext(patch("task_start.github.cli_credential", return_value=None))

    def failure(self, error, *, method="POST"):
        with patch("task_start.github.urlopen", side_effect=error) as transport:
            with self.assertRaises(TaskError) as caught:
                request("owner/repo/pulls", "dev-7-task", method=method, data={} if method != "GET" else None)
        transport.assert_called_once()
        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn(SECRET, rendered)
        self.assertNotIn("https://private.invalid", rendered)
        message = str(caught.exception)
        self.assertIn("GH_TOKEN", message)
        if method != "GET":
            self.assertIn("rerun task pr to discover and reuse", message)
        return message

    def http_error(self, code, *, payload=None, headers=None):
        return HTTPError("https://private.invalid/" + SECRET, code, SECRET,
                         headers or {"X-Private": SECRET}, io.BytesIO(json.dumps(
                             payload if payload is not None else {"message": SECRET}).encode()))

    def test_live_fine_grained_token_permission_rejection_is_actionable_and_safe(self):
        for method in ("POST", "PATCH"):
            with self.subTest(method=method):
                error = self.http_error(403, payload={
                    "message": "Resource not accessible by personal access token", "private": SECRET},
                    headers={"X-Accepted-GitHub-Permissions": "pull_requests=write", "X-Private": SECRET})
                message = self.failure(error, method=method)
                self.assertIn("HTTP 403", message)
                self.assertIn("insufficient token permissions", message)
                self.assertIn("Pull requests: write", message)

    def test_http_categories_give_safe_recovery_guidance(self):
        for code, expected in ((400, "malformed or unsupported API request"), (401, "authentication rejected"),
                               (403, "forbidden"), (404, "repository or endpoint unavailable"),
                               (405, "malformed or unsupported API request"), (409, "state conflict"),
                               (415, "malformed or unsupported API request"), (422, "validation failed"),
                               (429, "rate limit"), (500, "service failure"), (502, "service failure"),
                               (503, "service failure"), (301, "unexpected API response")):
            with self.subTest(code=code):
                message = self.failure(self.http_error(code))
                self.assertIn(f"HTTP {code}", message)
                self.assertIn(expected, message)

    def test_rate_limit_and_sso_are_distinguished_from_token_permissions(self):
        for headers, payload, expected in (
                ({"X-RateLimit-Remaining": "0"}, None, "rate limit"),
                ({"Retry-After": SECRET}, None, "rate limit"),
                ({}, {"message": "You have exceeded a secondary rate limit. " + SECRET}, "rate limit"),
                ({"X-GitHub-SSO": "required; url=https://private.invalid/" + SECRET}, None, "SSO authorization required")):
            with self.subTest(expected=expected):
                self.assertIn(expected, self.failure(self.http_error(403, headers=headers, payload=payload)))

    def test_validation_fields_are_allowlisted_without_echoing_private_values(self):
        payload = {"message": SECRET, "errors": [
            {"field": "head", "code": "invalid", "value": SECRET, "message": SECRET},
            {"field": "base", "code": "missing_field"},
            {"field": SECRET, "code": "invalid"}, {"field": "title", "code": SECRET},
            {"field": [SECRET], "code": {"private": SECRET}}, SECRET]}
        message = self.failure(self.http_error(422, payload=payload))
        self.assertIn("head: invalid", message)
        self.assertIn("base: missing_field", message)
        for text, expected in (("A pull request already exists for " + SECRET, "PR already exists"),
                               ("No commits between " + SECRET, "no commits between")):
            with self.subTest(expected=expected):
                payload = {"message": "Validation Failed", "errors": [{"code": "custom", "message": text}]}
                self.assertIn(expected, self.failure(self.http_error(422, payload=payload)))

    def test_malformed_or_unreadable_error_body_keeps_http_status(self):
        for content in (b"not JSON " + SECRET.encode(), b'"' + b"x" * 16384 + b'"', b"[]", b"null"):
            error = HTTPError("https://private.invalid", 403, SECRET, {}, io.BytesIO(content))
            self.assertIn("HTTP 403", self.failure(error))
        stream = io.BytesIO()
        with patch.object(stream, "read", side_effect=IncompleteRead(SECRET.encode())):
            error = HTTPError("https://private.invalid", 503, SECRET, {}, stream)
            self.assertIn("HTTP 503", self.failure(error))
        self.assertTrue(stream.closed)

    def test_transport_categories_never_echo_exception_details(self):
        for error, expected in (
                (URLError(socket.gaierror(-2, SECRET)), "DNS lookup failed"),
                (URLError(ssl.SSLCertVerificationError(1, SECRET)), "TLS certificate verification failed"),
                (URLError(ssl.SSLError(1, SECRET)), "TLS connection failed"),
                (URLError(TimeoutError(SECRET)), "timed out"), (TimeoutError(SECRET), "timed out"),
                (ConnectionResetError(SECRET), "transport failed"),
                (URLError(SECRET), "transport failed"), (RemoteDisconnected(SECRET), "transport failed"),
                (IncompleteRead(SECRET.encode()), "transport failed"),
                (InvalidURL(SECRET), "invalid API request configuration"),
                (ValueError(SECRET), "invalid API request encoding")):
            with self.subTest(error=type(error).__name__):
                self.assertIn(expected, self.failure(error))

    def test_malformed_json_after_write_retains_uncertain_outcome(self):
        for body in (SECRET.encode(), b'"\xff"'):
            with patch("task_start.github.urlopen", return_value=io.BytesIO(body)) as transport:
                with self.assertRaises(TaskError) as caught:
                    request("owner/repo/pulls", "dev-7-task", method="POST", data={})
            transport.assert_called_once()
            self.assertIn("malformed JSON API response", str(caught.exception))
            self.assertIn("write may already have reached GitHub", str(caught.exception))
            self.assertNotIn(SECRET, "".join(traceback.format_exception(caught.exception)))

    def test_history_diagnostics_keep_the_cause_and_read_permission_guidance(self):
        message = self.failure(self.http_error(403, payload={"message": "Resource not accessible by integration"}), method="GET")
        self.assertIn("Cannot establish GitHub PR history", message)
        self.assertIn("HTTP 403", message)
        self.assertIn("Pull requests: read", message)
        self.assertNotIn("write may already", message)

    def test_credential_precedence_and_empty_fallback_match_actual_header(self):
        for primary, secondary, source, value in ((SECRET, "secondary", "GH_TOKEN", SECRET),
                                                 ("", "secondary", "GITHUB_TOKEN", "secondary")):
            with self.subTest(source=source), patch.dict(os.environ, {"GH_TOKEN": primary, "GITHUB_TOKEN": secondary}), \
                    patch("task_start.github.urlopen", return_value=io.BytesIO(b"[]")) as transport:
                request("owner/repo/pulls", "dev-7-task")
                outgoing = transport.call_args.args[0]
                self.assertEqual(outgoing.get_header("Authorization"), "Bearer " + value)
                self.assertEqual(outgoing.full_url, "https://api.github.com/repos/owner/repo/pulls")
                self.assertEqual(api_credential()[0], source)

    def test_missing_credentials_allow_reads_but_refuse_writes_before_transport(self):
        with patch.dict(os.environ, {"GH_TOKEN": "", "GITHUB_TOKEN": ""}):
            with patch("task_start.github.urlopen", return_value=io.BytesIO(b"[]")) as transport:
                request("owner/repo/pulls", "dev-7-task")
                self.assertIsNone(transport.call_args.args[0].get_header("Authorization"))
            for method in ("POST", "PATCH"):
                with patch("task_start.github.urlopen") as transport, self.assertRaisesRegex(TaskError, "authentication is missing"):
                    request("owner/repo/pulls", "dev-7-task", method=method, data={})
                transport.assert_not_called()

    def test_anonymous_history_failure_explains_how_to_supply_api_credentials(self):
        with patch.dict(os.environ, {"GH_TOKEN": "", "GITHUB_TOKEN": ""}), \
                patch("task_start.github.urlopen", side_effect=self.http_error(404)):
            with self.assertRaises(TaskError) as caught:
                request("owner/repo/pulls", "dev-7-task")
        self.assertIn("unauthenticated", str(caught.exception))
        self.assertIn("gh auth login --hostname github.com", str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))

    def test_malformed_selected_credential_is_refused_without_fallback_or_disclosure(self):
        for character in (" ", "\t", "\r", "\n", "\x7f", "\u00e9", "\udcff"):
            with patch.dict(os.environ, {"GH_TOKEN": SECRET + character, "GITHUB_TOKEN": "valid-fallback"}), \
                    patch("task_start.github.urlopen") as transport:
                with self.assertRaisesRegex(TaskError, "credential GH_TOKEN") as caught:
                    request("owner/repo/pulls", "dev-7-task")
            transport.assert_not_called()
            self.assertNotIn(SECRET, "".join(traceback.format_exception(caught.exception)))

    def test_unserializable_request_is_refused_before_transport_without_disclosure(self):
        with patch("task_start.github.urlopen") as transport:
            with self.assertRaisesRegex(TaskError, "Cannot prepare GitHub API request") as caught:
                request("owner/repo/pulls", "dev-7-task", method="POST", data={SECRET: object()})
        transport.assert_not_called()
        self.assertNotIn(SECRET, "".join(traceback.format_exception(caught.exception)))
