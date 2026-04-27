# DraftRefine Vercel deployment

## Target shape

- One Vercel project
- Static Vite frontend served from `dist`
- FastAPI backend served from `api/index.py`
- PostgreSQL used in production
- Vercel Blob used for uploaded assets and cached full text PDFs

## Required environment variables

- `DRAFTREFINE_DATABASE_URL`
  - Prefer the Vercel-provisioned Postgres URL
- `DEEPSEEK_API_KEY` or `DRAFTREFINE_DEEPSEEK_API_KEY`
- `QWEN_API_KEY` or `DRAFTREFINE_QWEN_API_KEY` (optional)
- `SEMANTIC_SCHOLAR_API_KEY` (optional)
- `BLOB_READ_WRITE_TOKEN`
- `DRAFTREFINE_ALLOWED_ORIGINS`
  - Needed only if frontend and backend are split across origins

## Vercel project settings

- Framework preset: `Vite`
- Build command: `npm run build`
- Output directory: `dist`
- Root directory: repository root

## Storage setup

1. Connect a Postgres database in Vercel Storage.
2. Connect a private Blob store in Vercel Storage.
3. Copy or verify the generated environment variables in the project settings.

## Data migration

If you want to carry the current local projects, literature library, and revision history to production:

1. Set `DRAFTREFINE_DATABASE_URL` to the target Postgres URL.
2. Set `BLOB_READ_WRITE_TOKEN` if local source files or literature PDFs should be copied to Blob during migration.
3. Run:

```bash
python -m backend.migrate_to_postgres
```

## Current production caveat

The current frontend upload UI is still disabled. The deployment path that is ready first is:

- create project with pasted text
- run diagnosis
- use literature search / indexing
- run the text workspace agent

The legacy raw file upload API still exists for local development and small files, but it should not be treated as the final large-file Vercel upload path yet.

## Verification checklist

1. Open `/`
2. Create a text-based project
3. Confirm `/api/health` returns `ok`
4. Run one agent rewrite
5. Run one literature search
6. Import a literature item
7. Fetch one open-access full text
