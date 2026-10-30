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
	"SAP Item",
	"SAP Project",
	"SAP Sales Stage",
	"SAP Lead Source",
	"SAP Department",
	"SAP Designation",
	"SAP Employee",
	"SAP Asset Category",
	"SAP Bank Account",
	"SAP Journal Entry",
)

MASTER_RANK = {name: rank for rank, name in enumerate(MASTER_MAPPINGS)}


def mapping_sort_key(mapping_name: str, original_position: int) -> tuple[int, int]:
	return (MASTER_RANK.get(mapping_name, len(MASTER_RANK)), original_position)
