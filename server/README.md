# Domain Intelligence Query Server

FastAPI server that lets you query your domain database via:
1. **Natural language** (AI converts it to SQL via OpenAI)
2. **Manual filters** (structured API with all filter options)

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in:
#   DB_PATH=E:\domains.db
#   OPENAI_API_KEY=sk-...
```

### 3. Run the server

```bash
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### 4. Open the frontend

Open `frontend.html` in your browser. Set API Base URL to `http://localhost:8000`.

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Server health check |
| GET | `/stats` | DB overview stats |
| POST | `/query/nl` | Natural language → SQL → results |
| POST | `/query/filter` | Structured filter query |
| GET | `/domain/{domain}` | Single domain lookup |
| GET | `/filters/options` | Distinct values for filter dropdowns |

---

## Natural Language Query Examples

```json
POST /query/nl
{
  "query": "show me active logistics companies in India with English titles",
  "limit": 100
}
```

```json
POST /query/nl
{
  "query": "exim domains that timed out ordered by domain name",
  "limit": 200
}
```

Response includes:
- `generated_sql` — the SQL the AI produced
- `explanation` — plain English explanation of what it does
- `results` — array of matching rows
- `elapsed_ms` — total time including AI call

---

## Filter Query Examples

```json
POST /query/filter
{
  "category": ["logistics", "exim"],
  "status_code": [200],
  "language": ["en", "en-US", "en-IN"],
  "has_description": true,
  "title_contains": "freight",
  "limit": 100
}
```

```json
POST /query/filter
{
  "status_code": [-1, -2],
  "category": ["exim"],
  "limit": 500
}
```

---

## Security Notes

- AI-generated SQL is validated before execution:
  - Only `SELECT` statements allowed
  - Forbidden keywords: `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, etc.
  - `PRAGMA query_only=ON` set at DB connection level
  - Row cap enforced regardless of what the AI generates
- CORS is open (`*`) — lock this down when deploying to production

---

## Cost Estimate (OpenAI)

Using `gpt-4o-mini`:
- ~500 input tokens + ~100 output tokens per NL query
- Cost: ~$0.0001 per query (fractions of a cent)
- 10,000 queries/month ≈ $1.00

Switch to `gpt-4o` in `.env` if query accuracy needs to improve.

---

## File Structure

```
domain-query-server/
├── main.py          # FastAPI app, all routes
├── ai_agent.py      # OpenAI NL→SQL conversion
├── db.py            # DB connection + safe query execution
├── models.py        # Pydantic request/response models
├── config.py        # Settings from .env
├── requirements.txt
├── .env.example
└── frontend.html    # Standalone query UI (open in browser)
```
