import frappe

from emkan_insights.emkan_insights.external_data_deletion import (
	enqueue_external_data_deletion_batch,
)
from emkan_insights.emkan_insights.scheduler import (
	SITE_PIPELINE_LOCK_TTL,
	fetch_external_site_configuration_sequence_job,
	sync_external_site_configuration_sequence_job,
)


SCHEDULER_JOB_ID = "external_site_configuration_fetch_and_sync"


def enqueue_external_site_configuration_scheduler():
	"""Queue fetch, sync, and deletion reconciliation for all external sites."""
	job = frappe.enqueue(
		"emkan_insights.emkan_insights.scheduler.external_site_configuration_scheduler."
		"run_external_site_configuration_scheduler",
		queue="long",
		timeout=SITE_PIPELINE_LOCK_TTL,
		job_id=SCHEDULER_JOB_ID,
		deduplicate=True,
	)
	return {
		"status": "queued" if job else "already_queued",
		"job_id": SCHEDULER_JOB_ID,
	}


def run_external_site_configuration_scheduler():
	"""Fetch and sync every external site, then queue deletion reconciliation."""
	config_names = frappe.get_all(
		"External Site Configuration",
		pluck="name",
		order_by="creation asc, name asc",
	)
	results = {
		"status": "complete",
		"configurations": len(config_names),
		"fetch": [],
		"sync": [],
		"deletion": None,
	}

	ready_to_sync = []
	for config_name in config_names:
		result = _run_configuration_phase(
			"fetch",
			config_name,
			fetch_external_site_configuration_sequence_job,
		)
		results["fetch"].append(result)
		if result["status"] == "complete":
			ready_to_sync.append(config_name)

	ready_to_delete = []
	for config_name in ready_to_sync:
		result = _run_configuration_phase(
			"sync",
			config_name,
			sync_external_site_configuration_sequence_job,
			enqueue_deletion=False,
		)
		results["sync"].append(result)
		if result["status"] == "complete":
			ready_to_delete.append(config_name)

	results["deletion"] = enqueue_external_data_deletion_batch(
		ready_to_delete,
		enqueue_after_commit=False,
	)

	if _has_incomplete_phase(results):
		results["status"] = "complete_with_errors"

	frappe.logger("external_sync").info(
		"External site configuration scheduler completed: %s",
		results,
	)
	return results


def _run_configuration_phase(phase, config_name, method, **kwargs):
	try:
		result = method(config_name, **kwargs) or {}
		return {
			"site_config": config_name,
			"status": result.get("status", "complete"),
			"result": result,
		}
	except Exception:
		frappe.db.rollback()
		traceback = frappe.get_traceback()
		frappe.log_error(
			title=f"External site scheduler {phase} failed: {config_name}"[:140],
			message=traceback,
		)
		return {
			"site_config": config_name,
			"status": "failed",
			"error": traceback,
		}


def _has_incomplete_phase(results):
	phase_results = results["fetch"] + results["sync"]
	return any(result["status"] != "complete" for result in phase_results)
