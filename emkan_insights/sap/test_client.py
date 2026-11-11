import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from unittest import TestCase
from unittest.mock import patch

import frappe
import requests

from emkan_insights.sap.services.client import SAPB1Client


class TestSAPB1Client(TestCase):
	def test_fetch_all_requests_larger_pages_and_restores_headers(self):
		client = SAPB1Client("https://sap.example.com", "company", "user", "secret")
		pages = iter([
			{"value": [{"Code": "A"}], "@odata.nextLink": "Items?$skip=1"},
			{"value": [{"Code": "B"}]},
		])

		def get_page(endpoint, params=None):
			self.assertEqual(client.session.headers["Prefer"], "odata.maxpagesize=200")
			return next(pages)

		with patch.object(client, "get", side_effect=get_page):
			self.assertEqual(client.fetch_all("Items"), [{"Code": "A"}, {"Code": "B"}])
		self.assertNotIn("Prefer", client.session.headers)

	def test_login_does_not_send_saved_session_cookie(self):
		client = SAPB1Client("https://sap.example.com", "company", "user", "secret", session_id="old-session")
		response = requests.Response()
		response.status_code = 200
		response._content = b'{"SessionId": "new-session"}'

		def post(url, **kwargs):
			self.assertIsNone(client.session.cookies.get("B1SESSION"))
			self.assertEqual(kwargs["json"], client.credentials)
			return response

		with patch.object(client.session, "post", side_effect=post):
			client.login()

		self.assertEqual(client.session.cookies.get("B1SESSION"), "new-session")

	def test_login_reports_sap_error_without_exposing_password(self):
		client = SAPB1Client("https://sap.example.com", "company", "user", "secret")
		response = requests.Response()
		response.status_code = 500
		response._content = json.dumps({
			"error": {"code": 305, "message": {"value": "Switch company error: -1102 secret <b>"}}
		}).encode()
		with patch.object(client.session, "post", return_value=response):
			with self.assertRaisesRegex(frappe.ValidationError, "SAP code 305.*Switch company error: -1102") as error:
				client.login()

		self.assertNotIn("secret", str(error.exception))
		self.assertIn("&lt;b&gt;", str(error.exception))

	def test_login_preserves_http_error_for_non_sap_response(self):
		client = SAPB1Client("https://sap.example.com", "company", "user", "secret")
		response = requests.Response()
		response.status_code = 500
		response._content = b"Internal Server Error"
		with patch.object(client.session, "post", return_value=response):
			with self.assertRaises(requests.HTTPError):
				client.login()

	def test_retries_transient_service_layer_errors(self):
		client = SAPB1Client("https://sap.example.com", "company", "user", "password")
		retry = client.session.get_adapter("https://sap.example.com").max_retries

		self.assertEqual(retry.total, 3)
		self.assertIn(500, retry.status_forcelist)
		self.assertIn(503, retry.status_forcelist)
		self.assertIn("GET", retry.allowed_methods)
		self.assertIn("POST", retry.allowed_methods)
		self.assertNotIn(401, retry.status_forcelist)
		self.assertFalse(retry.raise_on_status)

	def test_relogs_in_and_retries_get_after_401(self):
		client = SAPB1Client("https://sap.example.com", "company", "user", "password", session_id="old-session")
		unauthorized = requests.Response()
		unauthorized.status_code = 401
		unauthorized._content = b'{"error": "session expired"}'
		success = requests.Response()
		success.status_code = 200
		success._content = b'{"value": [{"Code": "1145000"}]}'
		login = requests.Response()
		login.status_code = 200
		login._content = b'{"SessionId": "new-session"}'

		with patch.object(client.session, "get", side_effect=[unauthorized, success]) as get:
			with patch.object(client.session, "post", return_value=login) as post:
				result = client.get("ChartOfAccounts")

		self.assertEqual(result["value"], [{"Code": "1145000"}])
		self.assertEqual(get.call_count, 2)
		post.assert_called_once()
		self.assertEqual(client.session.cookies.get("B1SESSION"), "new-session")

	def test_retries_500_three_times(self):
		class Handler(BaseHTTPRequestHandler):
			requests_seen = 0

			def do_GET(self):
				type(self).requests_seen += 1
				self.send_response(500 if self.requests_seen <= 3 else 200)
				self.end_headers()
				self.wfile.write(b'{"value": []}')

			def log_message(self, *args):
				pass

		server = HTTPServer(("127.0.0.1", 0), Handler)
		thread = Thread(target=server.serve_forever, daemon=True)
		thread.start()
		try:
			client = SAPB1Client(
				f"http://127.0.0.1:{server.server_port}", "company", "user", "password",
				session_id="existing-session",
			)
			self.assertEqual(client.get("ChartOfAccounts"), {"value": []})
			self.assertEqual(Handler.requests_seen, 4)
		finally:
			server.shutdown()
			server.server_close()
			thread.join()
