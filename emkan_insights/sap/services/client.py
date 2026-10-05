from __future__ import annotations

from time import monotonic
from typing import Any
from urllib.parse import urljoin

import frappe
import requests
from frappe.utils import cint
from frappe.utils.password import set_encrypted_password


class SAPB1Client:
	REAUTHENTICATE_STATUSES = {401, 403, 440}

	def __init__(
		self,
		base_url: str,
		company_db: str,
		username: str,
		password: str,
		*,
		verify_ssl: bool = False,
		session_timeout_minutes: int = 25,
		configuration_name: str | None = None,
		session_id: str | None = None,
	):
		self.base_url = self._normalize_base_url(base_url)
		self.credentials = {
			"CompanyDB": company_db,
			"UserName": username,
			"Password": password,
		}
		self.session_timeout_seconds = max(cint(session_timeout_minutes), 1) * 60
		self.session = requests.Session()
		self.session.verify = verify_ssl
		self.configuration_name = configuration_name
		self.logged_in_at: float | None = monotonic() if session_id else None
		if session_id:
			self.session.cookies.set("B1SESSION", session_id)

	@classmethod
	def from_configuration(cls, configuration_name: str) -> "SAPB1Client":
		configuration = frappe.get_doc("External SAP Configuration", configuration_name)
		return cls(
			configuration.base_url,
			configuration.company_db,
			configuration.username,
			configuration.get_password("password"),
			session_timeout_minutes=configuration.session_timeout_minutes or 25,
			configuration_name=configuration.name,
			session_id=configuration.get_password("session_id", raise_exception=False),
		)

	def login(self) -> dict[str, Any]:
		response = self.session.post(self._url("Login"), json=self.credentials, timeout=30)
		response.raise_for_status()
		payload = response.json()
		if session_id := payload.get("SessionId"):
			self.session.cookies.set("B1SESSION", session_id)
			self._save_session_id(session_id)
		self.logged_in_at = monotonic()
		return payload

	def get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
		self._ensure_session()
		response = self.session.get(self._url(endpoint), params=params, timeout=30)
		if response.status_code in self.REAUTHENTICATE_STATUSES:
			self.login()
			response = self.session.get(self._url(endpoint), params=params, timeout=30)
		response.raise_for_status()
		return response.json()

	def fetch_all(self, endpoint: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
		records = []
		next_endpoint: str | None = endpoint
		next_params = params

		while next_endpoint:
			payload = self.get(next_endpoint, params=next_params)
			page = payload.get("value", [])
			if not isinstance(page, list):
				raise ValueError("SAP OData response 'value' must be an array.")
			records.extend(page)
			next_endpoint = payload.get("@odata.nextLink") or payload.get("odata.nextLink")
			next_params = None

		return records

	def _ensure_session(self) -> None:
		if self.logged_in_at is None or monotonic() - self.logged_in_at >= self.session_timeout_seconds:
			self.login()

	def _save_session_id(self, session_id: str) -> None:
		if not self.configuration_name:
			return
		set_encrypted_password(
			"External SAP Configuration",
			self.configuration_name,
			session_id,
			fieldname="session_id",
		)
		frappe.db.set_value(
			"External SAP Configuration",
			self.configuration_name,
			"session_id",
			"********",
			update_modified=False,
		)

	def _url(self, endpoint: str) -> str:
		if endpoint.startswith(("http://", "https://")):
			return endpoint
		return urljoin(f"{self.base_url}/", endpoint.lstrip("/"))

	@staticmethod
	def _normalize_base_url(base_url: str) -> str:
		base_url = (base_url or "").rstrip("/")
		if not base_url:
			raise ValueError("SAP base URL is required.")
		if not base_url.endswith("/b1s/v1"):
			base_url = f"{base_url}/b1s/v1"
		return base_url
