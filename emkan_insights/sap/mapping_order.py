"""Place SAP master mappings before documents that refer to them."""

MASTER_MAPPINGS = (
	"SAP UOM",
	"SAP Item Group",
	"SAP Supplier Group",
	"SAP Customer Group",
	"SAP Account",
	"SAP Cost Center",
	"SAP Bank",
	"SAP Warehouse",
	"SAP Price List",
	"SAP Payment Term",
	"SAP Mode of Payment",
	"SAP Supplier",
	"SAP Customer",
	"SAP Asset Category",
	"SAP Item",
	"SAP Fixed Asset",
	"SAP Project",
	"SAP Sales Stage",
	"SAP Lead Source",
	"SAP Department",
	"SAP Designation",
	"SAP Employee",
	"SAP Bank Account",
	"SAP Journal Entry",
)

MASTER_RANK = {name: rank for rank, name in enumerate(MASTER_MAPPINGS)}

DOCUMENT_MAPPINGS = (
	"SAP Material Request",
	"SAP Supplier Quotation",
	"SAP Purchase Order",
	"SAP Purchase Down Payment",
	"SAP Purchase Receipt",
	"SAP Purchase Invoice",
	"SAP Quotation",
	"SAP Sales Order",
	"SAP Delivery Note",
	"SAP Sales Invoice",
)

DOCUMENT_RANK = {name: rank for rank, name in enumerate(DOCUMENT_MAPPINGS)}

PAYMENT_LEDGER_MAPPINGS = {
	"SAP Payment Incoming Allocation Detail",
	"SAP Payment Incoming Detail",
	"SAP Payment Outgoing Allocation Detail",
	"SAP Payment Outgoing Detail",
	"SAP Payment Purchase Down Payment Detail",
	"SAP Payment Purchase Invoice Detail",
	"SAP Payment Sales Invoice Detail",
}


def mapping_sort_key(mapping_name: str, original_position: int) -> tuple[int, int]:
	if mapping_name in MASTER_RANK:
		return (MASTER_RANK[mapping_name], original_position)
	if mapping_name in DOCUMENT_RANK:
		return (len(MASTER_RANK) + DOCUMENT_RANK[mapping_name], original_position)
	if mapping_name in PAYMENT_LEDGER_MAPPINGS:
		return (len(MASTER_RANK) + len(DOCUMENT_RANK) + 1, original_position)
	return (len(MASTER_RANK) + len(DOCUMENT_RANK), original_position)
