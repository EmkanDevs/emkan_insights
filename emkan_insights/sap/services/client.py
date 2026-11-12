from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from time import monotonic
from typing import Any
from urllib.parse import urljoin

import frappe
import requests
from frappe.utils import cint, escape_html
from frappe.utils.password import set_encrypted_password
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class SAPB1Client:
	REAUTHENTICATE_STATUSES = {401, 403, 440}
	# Page size adapts to how long SAP takes: wide documents (about 20 KB each) make a
	# 1,000-record page slower than the default read timeout.
	FETCH_PAGE_SIZE = 1000
	FETCH_PAGE_SIZE_START = 500
	MIN_PAGE_SIZE = 100
	SLOW_PAGE_SECONDS = 8
	FAST_PAGE_SECONDS = 2
	PAGE_TIMEOUT = 180

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
		self.read_timeout = 30
		self.session = requests.Session()
		adapter = HTTPAdapter(max_retries=Retry(
				total=3,
				status_forcelist=tuple(range(500, 600)),
				allowed_methods=frozenset({"GET", "POST"}),
				backoff_factor=0.5,
				raise_on_status=False,
			))
		self.session.mount("https://", adapter)
		self.session.mount("http://", adapter)
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
		# A saved SAP session must not be sent with a new login request.
		self.session.cookies.clear()
		self.logged_in_at = None
		response = self.session.post(self._url("Login"), json=self.credentials, timeout=30)
		try:
			response.raise_for_status()
		except requests.HTTPError as exc:
			try:
				error = response.json().get("error", {})
			except (ValueError, AttributeError):
				raise exc
			message = error.get("message", {}) if isinstance(error, dict) else {}
			if isinstance(message, dict):
				message = message.get("value")
			if not isinstance(message, str) or not message:
				raise exc
			password = self.credentials["Password"]
			if password:
				message = message.replace(password, "[redacted]")
			message = escape_html(message)
			code = escape_html(str(error.get("code", "unknown")))
			raise frappe.ValidationError(
				f"SAP Service Layer login failed (HTTP {response.status_code}, SAP code {code}): {message}"
			) from exc
		payload = response.json()
		if session_id := payload.get("SessionId"):
			self.session.cookies.set("B1SESSION", session_id)
			self._save_session_id(session_id)
		self.logged_in_at = monotonic()
		return payload

	def get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
		self._ensure_session()
		response = self.session.get(self._url(endpoint), params=params, timeout=self.read_timeout)
		if response.status_code in self.REAUTHENTICATE_STATUSES:
			self.login()
			response = self.session.get(self._url(endpoint), params=params, timeout=self.read_timeout)
		response.raise_for_status()
		return response.json()

	def fetch_all(
		self,
		endpoint: str,
		params: dict[str, Any] | None = None,
		max_records: int | None = None,
		workers: int = 1,
		order_by: str | None = None,
	) -> list[dict[str, Any]]:
		if workers > 1 and order_by and max_records is None and not endpoint.startswith(("http://", "https://")):
			try:
				return self._fetch_all_parallel(endpoint, params, workers, order_by)
			except Exception:
				# Any problem (unsupported $orderby, expired session, throttling) falls back to
				# the sequential path below, which handles re-login and retries.
				frappe.log_error(title="SAP parallel fetch fell back to sequential"[:140], defer_insert=True)
		records = []
		next_endpoint: str | None = endpoint
		next_params = params

		previous_prefer = self.session.headers.get("Prefer")
		previous_timeout = self.read_timeout
		page_size = self.FETCH_PAGE_SIZE_START
		self.session.headers["Prefer"] = f"odata.maxpagesize={page_size}"
		self.read_timeout = self.PAGE_TIMEOUT
		try:
			while next_endpoint:
				started = monotonic()
				payload = self.get(next_endpoint, params=next_params)
				elapsed = monotonic() - started
				# The next link carries an absolute $skip, so the page size can change between pages.
				if elapsed > self.SLOW_PAGE_SECONDS and page_size > self.MIN_PAGE_SIZE:
					page_size = max(self.MIN_PAGE_SIZE, page_size // 2)
				elif elapsed < self.FAST_PAGE_SECONDS and page_size < self.FETCH_PAGE_SIZE:
					page_size = min(self.FETCH_PAGE_SIZE, page_size * 2)
				self.session.headers["Prefer"] = f"odata.maxpagesize={page_size}"
				page = payload.get("value", [])
				if not isinstance(page, list):
					raise ValueError("SAP OData response 'value' must be an array.")
				records.extend(page)
				if max_records is not None and len(records) >= max_records:
					return records[:max_records]
				next_endpoint = payload.get("@odata.nextLink") or payload.get("odata.nextLink")
				next_params = None
		finally:
			self.read_timeout = previous_timeout
			if previous_prefer is None:
				self.session.headers.pop("Prefer", None)
			else:
				self.session.headers["Prefer"] = previous_prefer

		return records

	def _fetch_all_parallel(
		self, endpoint: str, params: dict[str, Any] | None, workers: int, order_by: str,
	) -> list[dict[str, Any]]:
		"""Read the first page, then the following pages in waves of concurrent $skip requests.

		Pages are ordered by `order_by`, so the pages of one run line up. A page shorter than
		the first marks the end.
		"""
		self._ensure_session()
		query = {**(params or {}), "$orderby": order_by}
		headers = {"Prefer": f"odata.maxpagesize={self.FETCH_PAGE_SIZE_START}"}
		first = self.session.get(self._url(endpoint), params=query, headers=headers, timeout=self.PAGE_TIMEOUT)
		first.raise_for_status()
		payload = first.json()
		records = list(payload.get("value", []))
		if not (payload.get("@odata.nextLink") or payload.get("odata.nextLink")):
			return records
		page_size = len(records)
		if page_size == 0:
			return records

		def read_page(skip: int) -> list[dict[str, Any]]:
			session = requests.Session()
			session.headers.update(self.session.headers)
			session.headers.update(headers)
			session.cookies.update(self.session.cookies)
			session.verify = self.session.verify
			session.mount("https://", self.session.get_adapter("https://"))
			session.mount("http://", self.session.get_adapter("http://"))
			response = session.get(self._url(endpoint), params={**query, "$skip": skip}, timeout=self.PAGE_TIMEOUT)
			response.raise_for_status()
			return response.json().get("value", [])

		page = 1
		with ThreadPoolExecutor(max_workers=workers) as pool:
			while True:
				wave = list(pool.map(read_page, [(page + index) * page_size for index in range(workers)]))
				for rows in wave:
					records.extend(rows)
					if len(rows) < page_size:
						return records
				page += workers

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
		if not base_url.endswith(("/b1s/v1", "/b1s/v2")):
			base_url = f"{base_url}/b1s/v2"
		return base_url
