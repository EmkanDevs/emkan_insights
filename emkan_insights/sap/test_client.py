import json
from unittest import TestCase
from unittest.mock import patch

import frappe
import requests

from emkan_insights.sap.services.client import SAPB1Client


class TestSAPB1Client(TestCase):
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

		self.assertEqual(retry.total, 2)
		self.assertEqual(retry.status_forcelist, (502, 503, 504))
		self.assertIn("GET", retry.allowed_methods)
		self.assertIn("POST", retry.allowed_methods)
		self.assertNotIn(401, retry.status_forcelist)
