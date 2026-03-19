# S3 Object Storage — NexusRAG File Storage Migration

**Date:** 2026-03-19  
**Branch:** `feat/s3-minio-storage`  
**Status:** Implemented  

---

## Problem Statement

NexusRAG's original file storage had two critical issues:

### 1. Local Filesystem — Data Loss Risk

All uploaded files and Docling-extracted images were written to local directories inside the container:

```
backend/uploads/{filename}              # raw uploaded PDF / DOCX
backend/data/docling/kb_{id}/images/    # extracted page images
```

**Why this is dangerous:**
- Files disappear on every `docker compose down -v` or container restart
- Horizontal scaling is impossible (each replica has its own local disk)
- No backup/restore mechanism
- CI/CD pipelines can't share test fixtures between runners

### 2. Postgres Storing Full Markdown Content

Every processed document stored its entire parsed markdown in a `TEXT` column:

```sql
-- documents table — original schema
markdown_content TEXT  -- could be 500KB–5MB per row
```

**Why this is expensive:**
- Large rows slow down `SELECT *` queries that don't need the content
- Postgres TOAST management adds overhead for multi-MB cells
- Replication and WAL volume grows proportionally
- Full-table scans (e.g. listing documents) read unnecessary bytes

---

## Solution: S3-Compatible Object Storage

We migrated to **S3-compatible object storage** using:
- **MinIO** for local development (runs in Docker)
- **AWS S3** for production (drop-in replacement — same API)

### Architecture After Migration

```
┌──────────────────────────────────────────────────────────────┐
│                        API Request                           │
│  POST /upload → hash → S3 upload → DB record (keys only)    │
│  GET  /markdown → DB lookup → S3 download → stream to client │
│  GET  /images → DB lookup → generate presigned URL → return  │
│  GET  /presign → IDOR check → sign key → return short URL    │
└──────────────────────────────────────────────────────────────┘
           │                          │
    ┌──────▼──────┐           ┌───────▼──────┐
    │  PostgreSQL  │           │  MinIO / S3  │
    │  (metadata)  │           │  (file bytes) │
    │  + S3 keys   │           │               │
    └─────────────┘           └──────────────┘
```

### Two Buckets

| Bucket | Contents |
|---|---|
| `nexusrag-documents` | Raw uploaded files + parsed `.md` files |
| `nexusrag-images` | Docling-extracted PNG images |

Both buckets are **private** — no public access. All file access goes through the backend API which generates short-lived **pre-signed URLs**.

---

## Key Design Decisions

### Content-Addressable Keys (SHA-256)

Raw files and markdown use the SHA-256 hash of the file content as their S3 key:

```
kb_{workspace_id}/raw/{sha256hex}{ext}       # e.g. kb_1/raw/a3f...b9.pdf
kb_{workspace_id}/markdown/{sha256hex}.md    # e.g. kb_1/markdown/a3f...b9.md
kb_{workspace_id}/images/{uuid}.png          # images use UUID (generated, not uploaded)
```

**Why SHA-256 for raw/markdown files:**
- Identical files get the same key → deduplication is automatic
- `HEAD object` check before upload → zero-cost reuse of existing S3 objects
- If the same PDF is uploaded by 5 users in the same workspace, it is stored once in S3

**Why UUID for images:**
- Images are _generated_ by Docling, not uploaded by users
- Two runs of the same document might produce slightly different PNG bytes
- UUID prevents silent overwrite of images from a previous processing run

### Pre-signed URLs (Private Buckets)

Files are never directly accessible. The client always goes through the backend:

```
GET /api/v1/documents/{id}/presign?key=kb_1/images/abc.png&bucket=nexusrag-images
→ { "url": "http://minio:9000/nexusrag-images/kb_1/images/abc.png?X-Amz-Signature=...", "expires_at": "..." }
```

**Security:** The presign endpoint validates that the requested `key` belongs to the document being accessed, preventing IDOR attacks.

**TTL:** Default 1 hour (`S3_PRESIGN_EXPIRES_SECONDS=3600`), configurable per-request.

---

## What Changed

### Database Schema

| Table | Removed | Added |
|---|---|---|
| `documents` | `markdown_content TEXT` | `file_sha256 VARCHAR(64)` (indexed) |
| | | `s3_bucket VARCHAR(255)` |
| | | `s3_raw_key VARCHAR(1000)` |
| | | `s3_markdown_key VARCHAR(1000)` |
| `document_images` | `file_path VARCHAR(500)` | `s3_key VARCHAR(1000)` |
| | | `s3_bucket VARCHAR(255)` |

### New Files

| File | Purpose |
|---|---|
| `backend/app/services/storage_service.py` | `StorageService` — boto3 wrapper, presign, dedup check |
| `backend/alembic/versions/a1b2c3d4e5f6_s3_storage_migration.py` | DB migration |
| `docs/s3-feature.md` | This document |

### Modified Files

| File | Change |
|---|---|
| `backend/app/core/config.py` | Added `S3_*` env vars |
| `backend/app/models/document.py` | New S3 columns, removed old fields |
| `backend/app/services/models/parsed_document.py` | `ExtractedImage`: `file_path` → `s3_key + s3_bucket` |
| `backend/app/services/deep_document_parser.py` | Uploads images to S3 via tempfile; captions read from S3 |
| `backend/app/services/nexus_rag_service.py` | Uploads markdown to S3; S3 cleanup on delete |
| `backend/app/api/documents.py` | SHA-256 dedup upload; presign endpoint; S3 markdown streaming |
| `backend/app/main.py` | Removed static mount; added bucket bootstrap on startup |
| `docker-compose.yml` | Added MinIO service; removed local volume mounts |
| `.env.example` | Added S3 env var examples |
| `backend/requirements.txt` | Added `boto3`, `aioboto3` |

---

## Pros & Cons

### ✅ Pros

| Benefit | Detail |
|---|---|
| **Durability** | Files survive container restarts, deploys, and host migrations |
| **Cost efficiency** | Postgres rows are tiny (just keys); object storage is cheap at scale |
| **Deduplication** | Same file uploaded N times → stored once in S3, N DB records |
| **Horizontal scaling** | Any replica can read/write S3 — no shared local disk needed |
| **Security** | Buckets are private; short-lived presigned URLs limit exposure |
| **Separation of concerns** | Postgres for metadata/queries; S3 for blobs |
| **Audit trail** | S3 versioning and access logs available out-of-the-box |
| **Dev/Prod parity** | MinIO local = AWS S3 API compatible — no code changes needed |

### ⚠️ Cons / Tradeoffs

| Tradeoff | Mitigation |
|---|---|
| **Breaking change** | `markdown_content` removed — existing documents must be re-processed |
| **Network latency** | Every file read hits S3 (vs. Postgres query). Usually < 20ms on same network |
| **Presigned URL expiry** | Frontend must refresh URLs after TTL. Default 1h is generous |
| **Extra dependency** | Adds boto3 + MinIO to the stack — more moving parts |
| **Cold-start complexity** | Bucket bootstrap required on startup (already handled in `lifespan`) |
| **MinIO in local dev** | Docker compose adds one more service; minor machine resource cost |

---

## Breaking Change — Re-processing Required

> [!CAUTION]
> The `markdown_content` column has been **dropped**. Any document that was
> previously indexed will have `s3_markdown_key = NULL`.
>
> **To read the markdown** of old documents, you must re-process them:
> `POST /api/v1/documents/{id}/process`
>
> Old images are also gone (local `file_path` removed). Re-processing
> will re-extract and upload them to S3.

---

## How to Run Locally

```bash
# 1. Start all services (includes MinIO on port 9000)
docker compose up -d

# 2. MinIO Console — confirm buckets were auto-created
open http://localhost:9001
# Login: minioadmin / minioadmin

# 3. Upload a document
curl -X POST http://localhost:8080/api/v1/documents/upload/1 \
  -F "file=@sample.pdf"

# 4. Process it
curl -X POST http://localhost:8080/api/v1/documents/1/process

# 5. Get a presigned URL for an extracted image
curl "http://localhost:8080/api/v1/documents/1/presign?key=kb_1/images/abc.png&bucket=nexusrag-images"
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `S3_ENDPOINT_URL` | `http://localhost:9000` | MinIO / S3 endpoint |
| `S3_ACCESS_KEY_ID` | `minioadmin` | Access key |
| `S3_SECRET_ACCESS_KEY` | `minioadmin` | Secret key |
| `S3_REGION` | `us-east-1` | AWS region (MinIO ignores this) |
| `S3_BUCKET_DOCUMENTS` | `nexusrag-documents` | Bucket for raw + markdown files |
| `S3_BUCKET_IMAGES` | `nexusrag-images` | Bucket for extracted images |
| `S3_PRESIGN_EXPIRES_SECONDS` | `3600` | Pre-signed URL TTL (seconds) |
