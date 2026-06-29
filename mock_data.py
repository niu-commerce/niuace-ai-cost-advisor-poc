CONTRACT = {
    "name": "Hana Parcel C",
    "contractor": "ABC Construction Sdn Bhd",
    "contract_value": "RM58.3 Million",
    "contract_value_short": "RM58.3M",
    "bq_items": 3842,
    "business_unit": "Eco Sanctuary",
    "category_of_work": "Building Works",
    "awarded_date": "2026-06-27",
}

KPI_CARDS = [
    {"label": "Contract Value", "value": "RM58.3M", "tone": "primary"},
    {"label": "Historical Contracts Analysed", "value": "1,000", "tone": "neutral"},
    {"label": "Historical BQ Items Compared", "value": "2.8M", "tone": "neutral"},
    {"label": "Matching BQ Items", "value": "96%", "tone": "success"},
    {"label": "Confidence Score", "value": "97%", "tone": "success"},
    {"label": "Potential Saving", "value": "RM462,000", "tone": "warning"},
    {"label": "High Risk Items", "value": "12", "tone": "danger"},
    {"label": "Estimated Review Time", "value": "3 minutes", "tone": "primary"},
]

HIGH_RISK_ITEMS = [
    {
        "id": "waterproofing",
        "bq_item": "Waterproofing Membrane",
        "unit": "m2",
        "current_rate": "RM158",
        "historical_average": "RM132",
        "difference": "+20%",
        "risk_level": "Medium",
        "recommendation": "Request justification",
    },
    {
        "id": "ceramic-floor-tile",
        "bq_item": "Ceramic Floor Tile 600x600",
        "unit": "m2",
        "current_rate": "RM94",
        "historical_average": "RM83",
        "difference": "+13%",
        "risk_level": "Medium",
        "recommendation": "Review specification",
    },
    {
        "id": "aluminium-window-frame",
        "bq_item": "Aluminium Window Frame",
        "unit": "m2",
        "current_rate": "RM685",
        "historical_average": "RM673",
        "difference": "+2%",
        "risk_level": "Low",
        "recommendation": "Acceptable",
    },
    {
        "id": "reinforcement-steel-bar",
        "bq_item": "Reinforcement Steel Bar",
        "unit": "ton",
        "current_rate": "RM3,920",
        "historical_average": "RM3,870",
        "difference": "+1%",
        "risk_level": "Low",
        "recommendation": "Acceptable",
    },
]

ITEM_DETAILS = {
    "waterproofing": {
        "title": "Waterproofing Membrane Analysis",
        "current_rate": "RM158/m2",
        "historical_average": "RM132/m2",
        "lowest_historical_rate": "RM120/m2",
        "highest_historical_rate": "RM145/m2",
        "difference": "+20%",
        "historical_records_found": 127,
        "ai_explanation": (
            "Current rate is approximately 20% above the historical average. "
            "Most comparable EcoWorld projects fall between RM128 and RM138 per m2. "
            "Recommend requesting supporting justification before management approval."
        ),
        "similar_projects": [
            {"project": "Hana Parcel B", "contractor": "XYZ Builder", "rate": "RM132", "similarity": "97%"},
            {"project": "Ember Phase 2", "contractor": "YYY Contractor", "rate": "RM134", "similarity": "95%"},
            {"project": "Begonia Residence", "contractor": "ZZZ Construction", "rate": "RM130", "similarity": "93%"},
        ],
        "evidence": [
            {"label": "Consultant justification", "status": "Missing"},
            {"label": "Supplier quotation", "status": "Missing"},
            {"label": "Specification comparison", "status": "Available"},
            {"label": "Historical benchmark", "status": "Available"},
        ],
    }
}

DEFAULT_AI_RESPONSE = (
    "Possible reasons include imported material, new specification, smaller quantity, "
    "or complex detailing. However, consultant justification and supplier quotation are missing. "
    "Recommendation: request supporting documents before approval."
)
