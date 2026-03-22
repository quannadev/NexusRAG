# Tenant Isolation

NexusRAG supports a **3-tier data hierarchy** that enables true multi-tenant deployments within a single workspace instance.

```
Workspace (knowledge base)
├── Tenant A (e.g. bot_sales)
│   ├── Documents: report_q1.pdf, pricing.pdf
│   ├── Vector store: ChromaDB where={tenant_id: "bot_sales"}
│   ├── Knowledge graph: working_dir/kb_1__t_bot_sales/
│   └── S3: kb_1/tenant_bot_sales/raw/...
└── Tenant B (e.g. bot_support)
    ├── Documents: faq.pdf, manual.pdf
    ├── Vector store: ChromaDB where={tenant_id: "bot_support"}
    ├── Knowledge graph: working_dir/kb_1__t_bot_support/
    └── S3: kb_1/tenant_bot_support/raw/...
```

Each tenant has **fully isolated data at every layer**: vector store, knowledge graph, S3 storage, and database. A query from Tenant A will **never** return results from Tenant B.

---

## Isolation Guarantees

| Layer | Mechanism | Guarantee |
|---|---|---|
| **Vector store** | Auto-injected `where: {tenant_id: "$eq"}` ChromaDB filter | Chunks tagged with Tenant A are invisible to Tenant B queries |
| **Knowledge graph** | Separate LightRAG working directory per tenant | KG entities from Tenant A are never returned to Tenant B |
| **S3 storage** | Tenant-prefixed object keys | Files are path-separated; deployable with prefix-based IAM policies |
| **Database** | `tenant_id` column + composite index `(workspace_id, tenant_id)` | Dedup checks are strictly tenant-scoped |

---

## Backward Compatibility

`tenant_id=null` is the default and behaves identically to the pre-tenant mode — no change to existing behavior, no data migration required.

---

## API Reference

### Upload a document to a tenant

```http
POST /api/v1/documents/upload/{workspace_id}
Content-Type: multipart/form-data

file=<binary>
tenant_id=bot_sales           # optional — omit for workspace-global
custom_metadata=[{"key":"year","value":"2025"}]  # optional
```

**Example:**
```bash
curl -X POST http://localhost:8080/api/v1/documents/upload/1 \
  -F "file=@report.pdf" \
  -F "tenant_id=bot_sales"
```

---

### List documents (with optional tenant filter)

```http
GET /api/v1/documents/workspace/{workspace_id}?tenant_id=bot_sales
```

Omit `tenant_id` to list all documents in the workspace regardless of tenant.

---

### Query documents (tenant-scoped)

```http
POST /api/v1/rag/query/{workspace_id}
Content-Type: application/json

{
  "question": "What is the Q1 revenue?",
  "tenant_id": "bot_sales"
}
```

Both the vector search and knowledge graph query are automatically scoped to `bot_sales`. Omitting `tenant_id` queries across all tenants.

---

### Streaming chat (tenant-scoped)

```http
POST /api/v1/rag/chat/{workspace_id}/stream
Content-Type: application/json

{
  "message": "Summarize the pricing document",
  "tenant_id": "bot_sales"
}
```

---

### List tenants in a workspace

```http
GET /api/v1/workspaces/{workspace_id}/tenants
```

**Response:**
```json
{
  "workspace_id": 1,
  "total": 2,
  "tenants": [
    { "tenant_id": null,        "document_count": 3, "indexed_count": 3 },
    { "tenant_id": "bot_sales", "document_count": 5, "indexed_count": 4 },
    { "tenant_id": "bot_support","document_count": 2, "indexed_count": 2 }
  ]
}
```

`tenant_id: null` represents workspace-global documents (uploaded without a tenant).

---

### Delete all data for a tenant

> ⚠️ **Destructive and irreversible.** Deletes all indexed data for the tenant.

```http
DELETE /api/v1/workspaces/{workspace_id}/tenants/{tenant_id}
```

**What is deleted:**
1. ChromaDB vector chunks (`where tenant_id=$eq`)
2. LightRAG KG directory for the tenant
3. S3 objects: raw files, parsed markdown, extracted images
4. Database records: documents and associated images

**Response:**
```json
{
  "deleted_documents": 5,
  "workspace_id": 1,
  "tenant_id": "bot_sales",
  "detail": "Successfully deleted all data for tenant 'bot_sales'."
}
```

---

## S3 Key Layout

Object keys encode both workspace and tenant, making storage layout self-documenting:

```
kb_{workspace_id}/raw/{sha256}.pdf                  ← global (no tenant)
kb_{workspace_id}/tenant_{id}/raw/{sha256}.pdf      ← tenant raw file
kb_{workspace_id}/tenant_{id}/markdown/{sha256}.md  ← parsed markdown
kb_{workspace_id}/tenant_{id}/images/{uuid}.png     ← extracted image
```

---

## Use Cases

| Scenario | tenant_id | Example |
|---|---|---|
| Single knowledge base, no isolation needed | omit | Default workspace mode |
| Multiple bots sharing one workspace | per-bot string | `bot_sales`, `bot_support` |
| Department-level isolation | department name | `hr`, `finance`, `engineering` |
| Customer data isolation (SaaS) | customer ID | `cust_123`, `cust_456` |

---

## Implementation Notes

- **No extra infrastructure** — Isolation is enforced in software via ChromaDB `where` filters and separate LightRAG working directories. No separate databases or collections per tenant.
- **Dedup is tenant-scoped** — If two tenants upload the same file, the S3 object is shared (content-addressed by SHA-256) but the DB record and vector chunks are separate per tenant.
- **KG directory naming** — `kb_{workspace_id}__t_{tenant_id}/` (double underscore separates workspace from tenant for easy globbing).
