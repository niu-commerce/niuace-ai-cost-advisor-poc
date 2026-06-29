import os
from decimal import Decimal

from db import (
    fetch_contract_bq_items,
    fetch_item_benchmark,
    fetch_latest_contract,
    fetch_similar_project_rates,
    mysql_enabled,
)
from mock_data import CONTRACT, HIGH_RISK_ITEMS, ITEM_DETAILS, KPI_CARDS


def get_analysis(company_id=None, letter_award_id=None):
    mode = os.getenv("DATA_MODE", "mock").lower()
    default_company_id = int(os.getenv("MYSQL_DEFAULT_COMPANY_ID", "1452"))
    company_id = company_id or default_company_id

    if mode == "real" and mysql_enabled():
        real = build_real_analysis(company_id=company_id, letter_award_id=letter_award_id)
        if real:
            return real

    return mock_analysis()


def mock_analysis():
    return {
        "data_mode": "mock",
        "contract": CONTRACT,
        "kpis": KPI_CARDS,
        "assessment": {
            "badge": "Normal",
            "message": (
                "Most BQ items are within historical EcoWorld benchmark range. "
                "Management attention required only for 12 abnormal items."
            ),
        },
        "high_risk_items": HIGH_RISK_ITEMS,
        "item_details": ITEM_DETAILS,
        "recommendation": {
            "risk_level": "Medium",
            "reason": "12 abnormal items detected",
            "estimated_cost_impact": "RM462,000",
            "recommendation": "Approve after clarification on 12 highlighted items.",
            "confidence": "97%",
        },
    }


def build_real_analysis(company_id=1452, letter_award_id=None):
    contract = fetch_latest_contract(company_id=company_id, letter_award_id=letter_award_id)
    if not contract:
        return None

    items = fetch_contract_bq_items(contract["letter_award_id"], limit=800)
    benchmarked = []

    for item in items:
        benchmark = fetch_item_benchmark(
            company_id=company_id,
            item=item.get("item"),
            description=item.get("description"),
            uom_id=item.get("uom_id"),
            project_work_category_id=contract.get("project_work_category_id"),
            exclude_letter_award_id=contract["letter_award_id"],
        )
        if not benchmark or not benchmark.get("avg_rate"):
            continue

        current_rate = _decimal(item.get("effective_rate"))
        avg_rate = _decimal(benchmark.get("avg_rate"))
        if current_rate <= 0 or avg_rate <= 0:
            continue

        diff_pct = ((current_rate - avg_rate) / avg_rate) * Decimal("100")
        if diff_pct < Decimal("1"):
            continue

        risk_level = _risk_level(diff_pct)
        benchmarked.append({
            "source": item,
            "benchmark": benchmark,
            "current_rate": current_rate,
            "avg_rate": avg_rate,
            "diff_pct": diff_pct,
            "risk_level": risk_level,
            "cost_impact": _decimal(item.get("order_qty")) * (current_rate - avg_rate),
        })

    benchmarked.sort(key=lambda row: row["diff_pct"], reverse=True)
    high_risk = benchmarked[:12]
    matching_pct = _matching_percent(len(items), len(benchmarked))
    potential_saving = sum((row["cost_impact"] for row in high_risk if row["cost_impact"] > 0), Decimal("0"))
    confidence = _confidence_score(len(items), len(benchmarked), high_risk)

    high_risk_items = [_risk_row(row, idx) for idx, row in enumerate(high_risk)]
    item_details = _item_details(company_id, contract, high_risk, is_real=True)
    contract_value = _contract_value(contract)

    return {
        "data_mode": "real",
        "contract": {
            "name": contract.get("contract_no") or contract.get("loa_no") or "Selected Contract",
            "contractor": contract.get("contractor") or "-",
            "contract_value": _rm(contract_value),
            "contract_value_short": _rm_short(contract_value),
            "bq_items": len(items),
            "business_unit": contract.get("business_unit") or "-",
            "category_of_work": contract.get("category_of_work") or "-",
            "awarded_date": str(contract.get("awarded_date") or "-"),
        },
        "kpis": [
            {"label": "Contract Value", "value": _rm_short(contract_value), "tone": "primary"},
            {"label": "Historical Contracts Analysed", "value": "Live DB", "tone": "neutral"},
            {"label": "Historical BQ Items Compared", "value": f"{len(items):,}", "tone": "neutral"},
            {"label": "Matching BQ Items", "value": f"{matching_pct}%", "tone": "success"},
            {"label": "Confidence Score", "value": f"{confidence}%", "tone": "success"},
            {"label": "Potential Saving", "value": _rm(potential_saving), "tone": "warning"},
            {"label": "High Risk Items", "value": str(len(high_risk)), "tone": "danger"},
            {"label": "Estimated Review Time", "value": "3 minutes", "tone": "primary"},
        ],
        "assessment": {
            "badge": "Normal" if len(high_risk) <= 12 else "Review",
            "message": (
                f"{matching_pct}% of priced BQ items found benchmark evidence. "
                f"Management attention required for {len(high_risk)} abnormal items."
            ),
        },
        "high_risk_items": high_risk_items,
        "item_details": item_details,
        "recommendation": {
            "risk_level": "Medium" if high_risk else "Low",
            "reason": f"{len(high_risk)} abnormal items detected",
            "estimated_cost_impact": _rm(potential_saving),
            "recommendation": (
                "Approve after clarification on highlighted items."
                if high_risk else
                "No abnormal benchmark exceptions detected. Proceed with normal review."
            ),
            "confidence": f"{confidence}%",
        },
    }


def _risk_row(row, idx):
    item = row["source"]
    return {
        "id": f"real-{idx}",
        "bq_item": _item_title(item),
        "unit": item.get("unit") or "-",
        "current_rate": _rm(row["current_rate"]),
        "historical_average": _rm(row["avg_rate"]),
        "difference": f"+{row['diff_pct']:.0f}%",
        "risk_level": row["risk_level"],
        "recommendation": _recommendation(row["risk_level"]),
    }


def _item_details(company_id, contract, high_risk, is_real=False):
    if not high_risk:
        return {} if is_real else ITEM_DETAILS

    details = {}
    for idx, row in enumerate(high_risk):
        item = row["source"]
        benchmark = row["benchmark"]
        similar = fetch_similar_project_rates(
            company_id=company_id,
            item=item.get("item"),
            description=item.get("description"),
            uom_id=item.get("uom_id"),
            project_work_category_id=contract.get("project_work_category_id"),
            exclude_letter_award_id=contract["letter_award_id"],
            limit=3,
        )
        details[f"real-{idx}"] = {
            "title": f"{_item_title(item)} Analysis",
            "current_rate": f"{_rm(row['current_rate'])}/{item.get('unit') or 'unit'}",
            "historical_average": f"{_rm(row['avg_rate'])}/{item.get('unit') or 'unit'}",
            "lowest_historical_rate": f"{_rm(benchmark.get('min_rate'))}/{item.get('unit') or 'unit'}",
            "highest_historical_rate": f"{_rm(benchmark.get('max_rate'))}/{item.get('unit') or 'unit'}",
            "difference": f"+{row['diff_pct']:.0f}%",
            "historical_records_found": int(benchmark.get("records_found") or 0),
            "ai_explanation": (
                f"Current rate is approximately {row['diff_pct']:.0f}% above the historical average. "
                "Recommend requesting consultant justification, supplier quotation, and specification comparison "
                "before management approval."
            ),
            "similar_projects": [
                {
                    "project": s.get("project") or "-",
                    "contractor": s.get("contractor") or "-",
                    "rate": _rm(s.get("rate")),
                    "similarity": f"{max(86, 98 - i * 2)}%",
                }
                for i, s in enumerate(similar)
            ] or ITEM_DETAILS["waterproofing"]["similar_projects"],
            "evidence": [
                {"label": "Consultant justification", "status": "Missing"},
                {"label": "Supplier quotation", "status": "Missing"},
                {"label": "Specification comparison", "status": "Available"},
                {"label": "Historical benchmark", "status": "Available"},
            ],
        }
    return details


def _contract_value(contract):
    return _decimal(contract.get("awarded_contract_amount") or contract.get("original_total_amount"))


def _item_title(item):
    return (item.get("item") or item.get("description") or "BQ Item").strip()[:90]


def _risk_level(diff_pct):
    if diff_pct >= Decimal("25"):
        return "High"
    if diff_pct >= Decimal("10"):
        return "Medium"
    return "Low"


def _recommendation(risk_level):
    if risk_level == "High":
        return "Escalate before approval"
    if risk_level == "Medium":
        return "Request justification"
    return "Acceptable"


def _matching_percent(total_items, benchmarked_items):
    if total_items <= 0:
        return 0
    return min(99, round((benchmarked_items / total_items) * 100))


def _confidence_score(total_items, benchmarked_items, high_risk_items):
    if total_items <= 0:
        return 60
    base = 75 + min(22, round((benchmarked_items / total_items) * 30))
    penalty = min(8, len(high_risk_items) // 4)
    return max(60, min(99, base - penalty))


def _decimal(value):
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _rm(value):
    amount = _decimal(value)
    return f"RM{amount:,.2f}"


def _rm_short(value):
    amount = _decimal(value)
    if amount >= Decimal("1000000"):
        return f"RM{amount / Decimal('1000000'):,.1f}M"
    return _rm(amount)
