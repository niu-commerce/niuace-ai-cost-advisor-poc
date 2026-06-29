# NiuAce AI Cost Advisor POC

Small standalone FastAPI POC for an EcoWorld contract / VO AI cost benchmarking dashboard.

## Run locally

```powershell
cd C:\Users\lootoon\projects\niuace\niuace-ai-cost-advisor-poc
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
.\.venv\Scripts\python.exe -m uvicorn app:app --reload --host 127.0.0.1 --port 8010
```

Open:

```text
http://127.0.0.1:8010/
```

## Modes

- `DATA_MODE=mock`: dashboard uses the built-in sample contract and BQ benchmark data.
- `DATA_MODE=real`: dashboard reads the latest awarded contract and priced Contract BQ rows from MySQL.
- `AI_PROVIDER=mock`: no API key needed for chat responses.
- `AI_PROVIDER=openai`: set `OPENAI_API_KEY`.
- `AI_PROVIDER=claude`: set `ANTHROPIC_API_KEY`.
- `MYSQL_ENABLED=false`: mock-only demo.
- `MYSQL_ENABLED=true`: enables MySQL endpoints and real dashboard analysis.

## Real data setup

Update `.env`:

```env
DATA_MODE=real
MYSQL_ENABLED=true
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=your_mysql_user
MYSQL_PASSWORD=your_mysql_password
MYSQL_DATABASE=max_purchasing
MYSQL_DEFAULT_COMPANY_ID=1452
```

Then restart Uvicorn and open:

```text
http://127.0.0.1:8010/
```

Useful test endpoints:

```text
http://127.0.0.1:8010/api/analysis
http://127.0.0.1:8010/api/mysql/contract-bq-summary?company_id=1452&limit=20
```

Real mode flow:

1. Finds the latest awarded Letter of Award for company `1452`, unless `letter_award_id` is passed to `/api/analysis`.
2. Reads Contract BQ rows from `letter_award_tabs` and `letter_award_items`.
3. Uses an effective rate of `tender_rate`, otherwise `material_rate + service_rate`, ignoring zero placeholder rates.
4. Benchmarks each priced BQ item against historical awarded contracts with matching unit, category of work, and text token.
5. Returns the same dashboard shape as mock mode, so the frontend stays clickable for demos.

## Generate piling trade code suggestions

After loading rows into `max_purchasing.ai_bq_trade_code_suggestions`, run the worker from this folder:

```powershell
cd C:\Users\lootoon\projects\niuace\niuace-ai-cost-advisor-poc
.\.venv\Scripts\python.exe trade_code_worker.py --provider mock --limit 5 --dry-run
```

Use OpenAI:

```powershell
.\.venv\Scripts\python.exe trade_code_worker.py --provider openai --limit 50 --batch-size 10
```

Use Claude:

```powershell
.\.venv\Scripts\python.exe trade_code_worker.py --provider claude --limit 50 --batch-size 10
```

Useful rerun examples:

```powershell
# Process selected staging rows only
.\.venv\Scripts\python.exe trade_code_worker.py --provider openai --ids 101,102,103

# Reprocess rows that already have suggestions
.\.venv\Scripts\python.exe trade_code_worker.py --provider openai --include-processed --limit 20 --prompt-version piling-v2
```

The worker reads rows where `review_status` is `pending`, `needs_rerun`, `ai_error`, or `suggested_trade_code` is empty. It updates `suggested_trade_code`, `suggested_trade_name`, `spec_key`, `confidence`, `reasoning`, `review_status`, `prompt_version`, `ai_model`, `ai_run_id`, and `ai_processed_at`.

The worker prints timestamps and elapsed seconds for DB fetch, AI request, DB update, batch completion, and total runtime. To save a troubleshooting log:

```powershell
.\.venv\Scripts\python.exe trade_code_worker.py --provider openai --limit 50 --batch-size 10 *> trade-code-run.log
```

Each live run also inserts one row per BQ item into `max_purchasing.ai_bq_trade_code_suggestion_results`. Use this table to compare OpenAI, Claude, model changes, and prompt versions without overwriting previous results.

If the result table was already created before the unique key existed, add this once:

```sql
ALTER TABLE max_purchasing.ai_bq_trade_code_suggestion_results
ADD UNIQUE KEY uq_ai_bq_result_source_run (source_suggestion_id, ai_run_id);
```

Run OpenAI first:

```powershell
.\.venv\Scripts\python.exe trade_code_worker.py --provider openai --limit 50 --batch-size 10 --prompt-version piling-v1 --run-id openai-piling-v1
```

Then run Claude on the same source rows without overwriting the latest columns:

```powershell
.\.venv\Scripts\python.exe trade_code_worker.py --provider claude --include-processed --history-only --limit 50 --batch-size 10 --prompt-version piling-v1 --run-id claude-piling-v1
```

Compare provider output:

```sql
SELECT
    src.id AS source_suggestion_id,
    src.contract_no,
    src.item_ref_no,
    src.full_bq_description,
    src.uom_code,
    src.unit_rate,
    openai_result.suggested_trade_code AS openai_trade_code,
    openai_result.confidence AS openai_confidence,
    claude_result.suggested_trade_code AS claude_trade_code,
    claude_result.confidence AS claude_confidence,
    CASE
        WHEN openai_result.suggested_trade_code = claude_result.suggested_trade_code THEN 'same'
        ELSE 'different'
    END AS comparison_status
FROM max_purchasing.ai_bq_trade_code_suggestions src
LEFT JOIN max_purchasing.ai_bq_trade_code_suggestion_results openai_result
    ON openai_result.source_suggestion_id = src.id
   AND openai_result.ai_run_id = 'openai-piling-v1'
LEFT JOIN max_purchasing.ai_bq_trade_code_suggestion_results claude_result
    ON claude_result.source_suggestion_id = src.id
   AND claude_result.ai_run_id = 'claude-piling-v1'
ORDER BY comparison_status DESC, src.contract_no, src.item_ref_no;
```

For prompt `piling-v2`, the worker uses stricter canonical trade code rules. This prevents high-rate preliminary load test rows from being mixed into PDA rows:

```text
PDA test on 125 mm x 125 mm pile => PIL-PRC-125X125-PDA
Preliminary load test on 150 mm x 150 mm pile to 50 tons => PIL-PRC-150X150-LOAD-PRELIM-50T
Subsequent load test on 150 mm x 150 mm pile to 50 tons => PIL-PRC-150X150-LOAD-SUBSEQ-50T
Maintained load test on 150 mm x 150 mm pile to 50 tons => PIL-PRC-150X150-LOAD-MAINT-50T
Preliminary load test on 125 mm x 125 mm pile to 36 tons => PIL-PRC-125X125-LOAD-PRELIM-36T
```

Reprocess all extracted BQ rows and update the latest columns in `ai_bq_trade_code_suggestions`:

```powershell
.\.venv\Scripts\python.exe trade_code_worker.py --provider openai --include-processed --limit 100 --batch-size 10 --prompt-version piling-v2 --run-id openai-piling-v2-all
```

Reprocess all extracted BQ rows into history only, without changing latest columns:

```powershell
.\.venv\Scripts\python.exe trade_code_worker.py --provider openai --include-processed --history-only --limit 100 --batch-size 10 --prompt-version piling-v2 --run-id openai-piling-v2-all-history
```

Rerun selected suspicious rows into history:

```powershell
.\.venv\Scripts\python.exe trade_code_worker.py --provider openai --include-processed --history-only --ids 9,28,89,153,825,829,1523,1549,1731 --prompt-version piling-v2 --run-id openai-piling-v2-pda-check
```

The worker also applies deterministic canonical overrides after the AI response:

- PDA / Pile Driving Analysis without static-load wording => `PIL-PRC-{SIZE}-PDA`
- Preliminary load test wording => `PIL-PRC-{SIZE}-LOAD-PRELIM-{LOAD}`
- Subsequent load test wording => `PIL-PRC-{SIZE}-LOAD-SUBSEQ-{LOAD}`
- Maintained/static load test wording => `PIL-PRC-{SIZE}-LOAD-MAINT-{LOAD}`
- PDA / Pile Driving Analysis always wins over nearby generic load-test headers.
- Cut pile head to ground level due to obstruction => `PIL-PRC-{SIZE}-CUT-OBSTRUCTION`
- Supply, drive, and joint wording also override generic load-bearing-pile headers, so `m` rows do not become load/PDA test codes unless the item is truly a test.
- `working load`, `load bearing pile`, and `tonnes` are treated as pile capacity/spec words, not load-test activity, unless the item also says load test or loading arrangement.

Check the new classification:

```sql
SELECT
    source_suggestion_id,
    contract_no,
    item_ref_no,
    unit_rate,
    suggested_trade_code,
    suggested_trade_name,
    spec_key,
    confidence,
    reasoning
FROM max_purchasing.ai_bq_trade_code_suggestion_results
WHERE ai_run_id = 'openai-piling-v2-pda-check'
ORDER BY unit_rate DESC;
```

## Audit rate outliers

After trade codes are generated, run the rate auditor to find rows where a BQ item rate is far away from the common rate for the same trade code and UOM.

Dry-run latest table:

```powershell
.\.venv\Scripts\python.exe rate_outlier_auditor.py --source latest --ratio-threshold 2 --dry-run
```

Write audit results to `max_purchasing.ai_bq_trade_code_rate_audits`:

```powershell
.\.venv\Scripts\python.exe rate_outlier_auditor.py --source latest --ratio-threshold 2 --audit-run-id latest-rate-audit-v1
```

Audit a history run instead of latest columns:

```powershell
.\.venv\Scripts\python.exe rate_outlier_auditor.py --source history --ai-run-id openai-piling-v2c-history --ratio-threshold 2 --audit-run-id openai-piling-v2c-rate-audit
```

Review likely wrong trade codes:

```sql
SELECT
    audit_status,
    current_trade_code,
    inferred_trade_code,
    uom_code,
    unit_rate,
    common_rate,
    rate_ratio_to_common,
    contract_no,
    item_ref_no,
    contractor_name,
    prompt_rule_suggestion,
    audit_explanation,
    full_bq_description
FROM max_purchasing.ai_bq_trade_code_rate_audits
WHERE audit_run_id = 'latest-rate-audit-v1'
ORDER BY
    CASE audit_status
        WHEN 'likely_wrong_trade_code' THEN 1
        WHEN 'possible_trade_code_split' THEN 2
        ELSE 3
    END,
    rate_ratio_to_common DESC;
```

Summarize prompt tuning candidates:

```sql
SELECT
    audit_status,
    prompt_rule_suggestion,
    COUNT(*) AS affected_rows
FROM max_purchasing.ai_bq_trade_code_rate_audits
WHERE audit_run_id = 'latest-rate-audit-v1'
  AND prompt_rule_suggestion IS NOT NULL
GROUP BY audit_status, prompt_rule_suggestion
ORDER BY affected_rows DESC;
```

Generate an extra prompt-rules file from repeated audit findings:

```powershell
.\.venv\Scripts\python.exe prompt_tuning_generator.py --audit-run-id latest-rate-audit-v1 --output prompt_tuning_rules.txt
```

Preview before writing:

```powershell
.\.venv\Scripts\python.exe prompt_tuning_generator.py --audit-run-id latest-rate-audit-v1 --dry-run
```

`trade_code_worker.py` automatically appends `prompt_tuning_rules.txt` to the system prompt. To use a different rules file:

```powershell
$env:TRADE_CODE_PROMPT_RULES_FILE = "prompt_tuning_rules.txt"
.\.venv\Scripts\python.exe trade_code_worker.py --provider openai --include-processed --history-only --limit 100 --batch-size 10 --prompt-version piling-v3 --run-id openai-piling-v3-history
```
