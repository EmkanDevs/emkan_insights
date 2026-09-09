import frappe

from emkan_insights.emkan_insights.scheduler import (
	SITE_PIPELINE_LOCK_TTL,
	fetch_external_site_configuration_sequence_job,
	sync_external_site_configuration_sequence_job,
)


SCHEDULER_JOB_ID = "external_site_configuration_fetch_and_sync"


def enqueue_external_site_configuration_scheduler():
	"""Queue one fetch-and-sync job for all external site configurations."""
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
	"""Fetch every external site, then sync every successful fetch."""
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

	for config_name in ready_to_sync:
		results["sync"].append(
			_run_configuration_phase(
				"sync",
				config_name,
				sync_external_site_configuration_sequence_job,
			)
		)

	if _has_incomplete_phase(results):
		results["status"] = "complete_with_errors"

	frappe.logger("external_sync").info(
		"External site configuration scheduler completed: %s",
		results,
	)
	return results


def _run_configuration_phase(phase, config_name, method):
	try:
		result = method(config_name) or {}
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
