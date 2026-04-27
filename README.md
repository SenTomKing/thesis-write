# DraftRefine

DraftRefine is a web-first thesis refining MVP for internal testing. It focuses on revising an existing draft instead of generating a full paper from scratch.

## What is implemented

- Real FastAPI backend with SQLite persistence
- Upload flow for `txt`, `docx`, and `pdf` with fallback-to-pasted-text behavior
- Diagnostic console backed by stored issues instead of mock data
- Section-level AI rewrite pipeline with accept/reject and version recovery
- Supervisor comment import, auto-mapping, manual remapping, and status tracking
- Prompt registry under `backend/prompts` and Promptfoo regression scaffolding under `promptfoo`

## Stack

- Frontend: Vite + React + TypeScript + Zustand
- Backend: FastAPI
- LLM provider abstraction: DeepSeek first, Qwen fallback
- Storage/runtime target: local SQLite in development, PostgreSQL + Vercel Blob in deployment prep

## Local development

1. Install frontend dependencies

```bash
npm install
```

2. Install backend dependencies

```bash
python -m pip install -r backend/requirements.txt
```

3. Copy the environment template

```bash
copy .env.example .env
```

4. Start the backend

```bash
python -m backend.app
```

5. Start the frontend

```bash
npm run dev
```

The frontend uses `/api` in development and Vite proxies it to `http://127.0.0.1:8000`.

## Deployment

The repository now includes a Vercel deployment skeleton:

- static frontend build from `dist`
- FastAPI entrypoint at `api/index.py`
- environment-based API base URL
- PostgreSQL-compatible database bridge
- Blob-ready storage bridge

See `docs/deploy-vercel.md` for the current deployment checklist.

## Tests

```bash
npm test -- --run
python -m unittest discover backend/tests
```

## Prompt evaluation

The repository includes a Promptfoo config and datasets in `promptfoo/`. After adding Promptfoo locally, run:

```bash
npm run prompt:eval
```

Make sure your DeepSeek or Qwen API credentials are present in `.env` before running live prompt evaluations.

## Notes

- PDF parsing needs `pypdf`. If it is unavailable, the upload flow automatically falls back to pasted text instead of crashing the backend.
- The current production-first path is text-based project creation and the text workspace agent. The legacy raw file upload API still exists, but the frontend upload tab is not yet the final Vercel large-file flow.
