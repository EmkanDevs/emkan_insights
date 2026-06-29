import frappe
from frappe.model.document import Document


class GroupProjectWorkDistribution(Document):
    def validate(self):
        total = 0
        for row in self.project_executor_detail:
            total += row.execution_percentage or 0

        if total != 100:
            frappe.throw(f"Total Execution Percentage must be 100. Current: {total}")