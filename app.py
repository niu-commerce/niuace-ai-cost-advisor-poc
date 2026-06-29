from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ai_clients import ask_ai
from analysis_service import get_analysis, mock_analysis
import os

from db import (
    fetch_contract_bq_debug,
    fetch_contract_bq_summary,
    fetch_trade_code_library,
    fetch_trade_code_stats,
    fetch_trade_code_items,
    update_suggestion_trade_code,
    fetch_business_units,
    fetch_rate_analysis,
    fetch_contract_rate_items,
)

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="NiuAce AI Cost Advisor")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


class ChatRequest(BaseModel):
    question: str
    item_id: Optional[str] = "waterproofing"


class TradeCodeFix(BaseModel):
    trade_code: str
    trade_name: str = ""
    review_status: str = "approved"


class BQAnalyzeRequest(BaseModel):
    description: str
    business_unit: str = ""
    uom: str = ""


@app.get("/")
def index():
    return RedirectResponse(url="/trade-codes", status_code=302)


@app.get("/prompt-guide", response_class=HTMLResponse)
def prompt_guide_page():
    return (BASE_DIR / "templates" / "prompt_guide.html").read_text(encoding="utf-8")


@app.get("/api/prompt-config")
def api_prompt_config():
    from trade_code_worker import SYSTEM_PROMPT, active_system_prompt
    rules_file = os.getenv("TRADE_CODE_PROMPT_RULES_FILE", "prompt_tuning_rules.txt")
    tuning_rules = ""
    try:
        p = BASE_DIR / rules_file
        if p.exists():
            tuning_rules = p.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return {
        "system_prompt": SYSTEM_PROMPT,
        "active_prompt": active_system_prompt(),
        "tuning_rules": tuning_rules,
        "rules_file": rules_file,
    }


@app.get("/api/analysis")
def analysis(company_id: Optional[int] = None, letter_award_id: Optional[int] = None):
    return get_analysis(company_id=company_id, letter_award_id=letter_award_id)


@app.get("/api/mock/analysis")
def mock_analysis_endpoint():
    return mock_analysis()


@app.get("/api/contracts")
def contracts(company_id: int = 1452, limit: int = 50):
    rows = fetch_contract_bq_summary(company_id=company_id, limit=limit)
    return {
        "rows": rows,
    }


@app.get("/api/mysql/contract-bq-summary")
def mysql_contract_bq_summary(company_id: int = 1452, limit: int = 20):
    rows = fetch_contract_bq_summary(company_id=company_id, limit=limit)
    return {
        "enabled_result_count": len(rows),
        "rows": rows,
    }


@app.get("/api/debug/contract-bq/{letter_award_id}")
def debug_contract_bq(letter_award_id: int):
    return fetch_contract_bq_debug(letter_award_id)


@app.get("/trade-codes", response_class=HTMLResponse)
def trade_codes_page():
    return (BASE_DIR / "templates" / "trade_codes.html").read_text(encoding="utf-8")


@app.get("/api/trade-codes/stats")
def api_trade_code_stats():
    return fetch_trade_code_stats()


@app.get("/api/trade-codes")
def api_trade_codes(search: str = "", review_status: str = ""):
    rows = fetch_trade_code_library(search=search, review_status_filter=review_status)
    return {"rows": rows, "total": len(rows)}


@app.get("/api/trade-codes/{trade_code}/items")
def api_trade_code_items(trade_code: str, limit: int = 1000):
    rows = fetch_trade_code_items(trade_code=trade_code, limit=limit)
    return {"rows": rows, "total": len(rows), "trade_code": trade_code}


@app.patch("/api/trade-codes/{row_id}")
def api_fix_trade_code(row_id: int, payload: TradeCodeFix):
    ok = update_suggestion_trade_code(
        row_id=row_id,
        trade_code=payload.trade_code,
        trade_name=payload.trade_name,
        review_status=payload.review_status,
    )
    return {"success": ok, "id": row_id}


@app.get("/bq-analyzer", response_class=HTMLResponse)
def bq_analyzer_page():
    return (BASE_DIR / "templates" / "bq_analyzer.html").read_text(encoding="utf-8")


@app.get("/api/contract-rate-items")
def api_contract_rate_items(trade_code: str, contract_no: str, unit_rate: float):
    rows = fetch_contract_rate_items(trade_code=trade_code, contract_no=contract_no, unit_rate=unit_rate)
    return {"rows": rows, "total": len(rows)}


@app.get("/api/business-units")
def api_business_units():
    return {"units": fetch_business_units()}


@app.post("/api/analyze-bq")
def api_analyze_bq(payload: BQAnalyzeRequest):
    from trade_code_worker import suggest_trade_codes, apply_canonical_overrides, default_model

    provider = os.getenv("AI_PROVIDER", "mock").lower().strip()
    model = default_model(provider)

    fake_row = {
        "id": 0,
        "contract_no": "",
        "business_unit": payload.business_unit or "",
        "project_name": "",
        "project_shortname": "",
        "awarded_date": None,
        "contractor_name": "",
        "category_of_work": "",
        "letter_award_tab_id": None,
        "tab_name": "",
        "bq_item_id": None,
        "item_ref_no": 0,
        "bq_item_no": "",
        "parent_header_3": "",
        "parent_header_2": "",
        "parent_header_1": "",
        "bq_item_description": payload.description,
        "full_bq_description": payload.description,
        "uom_code": payload.uom or "",
        "quantity": None,
        "unit_rate": None,
        "tender_amount": None,
        "ai_matching_text": payload.description,
    }

    try:
        suggestions = suggest_trade_codes(provider, model, [fake_row], "piling-v1")
        suggestion = suggestions[0] if suggestions else {}
        suggestion = apply_canonical_overrides(fake_row, suggestion)
    except Exception as exc:
        suggestion = {
            "suggested_trade_code": "",
            "suggested_trade_name": "",
            "confidence": 0,
            "reasoning": f"AI error: {exc}",
        }

    trade_code = suggestion.get("suggested_trade_code", "")

    rate_all = fetch_rate_analysis(trade_code) if trade_code else {"stats": {}, "by_year": [], "recent": []}
    rate_same_bu = (
        fetch_rate_analysis(trade_code, payload.business_unit)
        if trade_code and payload.business_unit
        else {"stats": {}, "by_year": [], "recent": []}
    )

    return {
        "suggestion": suggestion,
        "trade_code": trade_code,
        "rate_all": rate_all,
        "rate_same_bu": rate_same_bu,
        "business_unit": payload.business_unit,
        "provider": provider,
        "model": model,
    }


@app.post("/api/chat")
def chat(payload: ChatRequest):
    analysis_data = get_analysis()
    item_details = analysis_data.get("item_details", {})
    selected_item = (
        item_details.get(payload.item_id or "")
        or item_details.get("waterproofing")
        or next(iter(item_details.values()), None)
    )
    context = {
        "contract": analysis_data.get("contract"),
        "selected_item": selected_item,
        "high_risk_items": analysis_data.get("high_risk_items", []),
    }
    return ask_ai(payload.question, context)
