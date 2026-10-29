# AI Invoice Auditor

An intelligent invoice processing system that automates vendor invoice validation. It monitors incoming invoices, extracts data, translates non-English documents, validates against purchase orders in the ERP system, generates detailed audit reports, and enables AI-powered question answering over invoice data.

Supports PDF, DOCX, PNG, JPG, and TIFF formats. Works with English, Spanish, and German languages.

---

## How it Works

The pipeline processes each invoice through a series of validation stages:

```
data/incoming/ → Monitor → Extract → Translate → Validate → Report → Index
                            ↓
                         PostgreSQL (audit trail)
                            ↓
                         Dashboard / RAG Query
```

Each stage writes results to the database, ensuring a complete audit trail even if processing fails partway through.

---

## Services

| Service | Role | Port |
|---------|------|------|
| **postgres** | Database: ERP data, audit logs, LangGraph checkpoints | 5432 |
| **mock-erp** | REST API to query purchase orders and vendor master data | 8001 |
| **qdrant** | Vector store for document embeddings and semantic search | 6333 |
| **redis** | Cache for LLM responses, translations, and RAG queries | 6379 |
| **agent** | Core processing engine (LangGraph workflows + API) | 8000 |
| **streamlit-ui** | Web dashboard for auditors | 8501 |

All services run locally. Containers communicate via internal Docker network; host ports are for external access only.

---

## Architecture

```
agent/                    LangGraph workflows, extraction, translation, validation
mock-erp/                 FastAPI service exposing ERP schema
streamlit-ui/             Interactive dashboard
database/                 PostgreSQL schemas, initialization scripts
config/                   Rules, prompts, and tolerance thresholds
qdrant/ redis/            Configuration files for those services
data/
  ├── incoming/           Drop invoices here
  └── reports/            Generated audit reports (JSON/HTML)
```

The system enforces separation at the database level: the agent's database role cannot access the ERP schema directly—it must go through the mock-erp REST API. This ensures a clean boundary between operational data and audit processes.

---

## Setup

### 1. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` and set:

- **Database passwords** (4 required): `POSTGRES_PASSWORD`, `ERP_DB_PASSWORD`, `AGENT_DB_PASSWORD`, `UI_DB_PASSWORD`
- **AWS credentials** (for Bedrock LLM and embeddings): `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
- **User ID** (for file permissions): `AGENT_UID`, `AGENT_GID` (run `id -u; id -g` to get these)

AWS credentials are optional: if not provided, the system degrades gracefully (translations weaken, manual review increases).

### 2. Start the Stack

```bash
docker compose up --build -d
docker compose logs -f agent
```

The first build takes a few minutes (tesseract OCR + language packs).

### 3. Access the Services

| Service | URL |
|---------|-----|
| Dashboard | http://localhost:8501 |
| Agent API | http://localhost:8000/docs |
| ERP API | http://localhost:8001/docs |
| Qdrant Console | http://localhost:6333/dashboard |

---

## Usage

### Process an Invoice

Copy an invoice file into `data/incoming/` and it will be picked up automatically within 10 seconds.

Or trigger manually:

```bash
curl -X POST http://localhost:8000/scan \
  -H 'content-type: application/json' \
  -d '{"force": true}'
```

### Upload via Dashboard

Go to http://localhost:8501, use the upload section to submit files and track processing status.

### Query Invoice Data

Ask questions about processed invoices using semantic search:

```bash
curl -X POST http://localhost:8000/rag/query \
  -H 'content-type: application/json' \
  -d '{"question": "Which invoices had discrepancies?"}'
```

The RAG system returns answers with confidence scores and document citations.

---

## Tuning

Configuration files in `config/` are mounted into the agent at runtime. Changes take effect after restart:

```bash
docker compose restart agent
```

- **rules.yaml** — Validation rules, severity levels, rejection thresholds
- **personas.yaml** — System prompts for different validation agents
- **prompts.yaml** — Message templates for LLM interactions

---

## Backup & Restore

### Backup

```bash
./database/backup.sh
```

Creates a PostgreSQL dump in `database/backups/` with all audit data, ERP records, and processing history.

### Restore After Data Loss

```bash
docker compose down -v                                    # Remove all volumes
docker compose up -d postgres                             # Start postgres with fresh schemas
./database/restore.sh database/backups/<db>_<timestamp>.dump
docker compose up -d                                      # Start remaining services
curl -X POST http://localhost:8000/admin/reindex          # Rebuild search index
```

Qdrant (vector index) can be rebuilt from PostgreSQL if lost; PostgreSQL is the source of truth.

---

## Development

### Validation Pipeline

The invoice validation logic is in `agent/app/`:

- **extraction/** — Document parsing (PDF, images, DOCX)
- **graph/** — LangGraph workflow orchestration
- **services/** — LLM, embeddings, ERP client, reporting
- **validation/** — Business rule checks and data quality validation
- **db/** — Database operations and audit trail
- **models.py** — Data structures
- **watcher.py** — File system monitoring

### Testing & Tools

```bash
python agent/tools/validate_embeddings.py      # Test AWS Bedrock connection
python agent/tools/check_parser.py             # Test document extraction
```

### Health Check

```bash
curl http://localhost:8000/health
```

Returns system status: database, embeddings, LLM connectivity, and cache health.

---

## Performance & Scalability

- **Single-pass extraction** — No LLM needed for basic parsing (uses pdfplumber, python-docx, tesseract)
- **LLM caching** — Translation and LLM responses cached in Redis with TTL
- **Concurrent processing** — Default: 2 invoices processed in parallel; adjust `WATCH_MAX_CONCURRENCY`
- **Connection pooling** — PostgreSQL pool defaults to 1-8 connections; tunable
- **Embedding model** — Bedrock by default (no local GPU needed); switch to fastembed for local embeddings

---

## Sample Data

`data/incoming/` includes test invoices demonstrating different edge cases:

- Clean English PDF
- Non-tabular line items (narrative descriptions)
- Spanish PDF with comma decimal separators
- German invoice
- Handwritten scan (requires OCR)
- Malformed PDF (missing fields)

Each invoice has a `.meta.json` file simulating email metadata.

---

## Troubleshooting

**Agent won't start**  
Check AWS credentials if using Bedrock. Verify database connectivity: `docker compose logs postgres`.

**Embeddings fail**  
Confirm AWS access to `amazon.titan-embed-text-v2`. Alternatively, switch to local embeddings: `EMBEDDING_PROVIDER=fastembed` (requires rebuilding the image).

**LLM responses weak or missing**  
Without AWS credentials, LLM features degrade but don't fail. Set AWS keys to enable full translation and severity grading.

**Qdrant index stale**  
Rebuild with: `curl -X POST http://localhost:8000/admin/reindex`

**Dashboard unresponsive**  
Ensure Streamlit has sufficient resources. Check `docker compose logs streamlit-ui`.

---

## License & Attribution

This project was developed as a capstone implementation of invoice automation patterns and multi-agent AI workflows.

