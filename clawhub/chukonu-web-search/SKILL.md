---
name: chukonu-web-search
description: Use the Chukonu Web Search service on port 8000 for agent-ready web, academic, and patent search evidence. Trigger when a task needs external, current, citable, academic, or patent information; return and reason from structured evidence, answerability gaps, and partial-failure diagnostics.
version: 0.1.0
metadata:
  openclaw:
    primaryEnv: CHUKONU_SEARCH_API_TOKEN
    envVars:
      - name: CHUKONU_SEARCH_MCP_URL
        required: false
        description: Optional MCP endpoint, for example https://search.example.com/mcp or http://localhost:8000/mcp.
      - name: CHUKONU_SEARCH_BASE_URL
        required: false
        description: Optional REST base URL, for example https://search.example.com or http://localhost:8000.
      - name: CHUKONU_SEARCH_API_TOKEN
        required: false
        description: Optional bearer token for the /mcp and /search endpoints. Never print or expose this token.
---

# Chukonu Web Search

Use this skill to search the Chukonu Web Search service and give the user evidence-backed answers. Prefer the configured MCP server when available; use the REST `/search` endpoint only as a fallback.

## Stable Capability

Use only the stable `search` capability.

PDF full-text extraction and continuation are not part of this skill version. Answer from the normal search evidence: web snippets/content, academic abstracts and metadata, and patent abstracts and metadata.

## Connection

Use one of these connection paths:

- MCP: `CHUKONU_SEARCH_MCP_URL`, typically `<base>/mcp`.
- REST fallback: `CHUKONU_SEARCH_BASE_URL`, then `POST <base>/search`.

If the service requires auth, send `Authorization: Bearer $CHUKONU_SEARCH_API_TOKEN`. Never reveal the token in logs, answers, examples, or error messages.

Do not call internal upstream services directly. The public product surface is the port 8000 service; do not query 9001 or 9101.

## Search Request

Call `search` with a concise natural-language query:

```json
{
  "query": "question or search terms",
  "top_k": 10,
  "include_academic": null,
  "include_patent": null,
  "rerank": null
}
```

Parameter guidance:

- `include_academic`: use `true` for paper, research, DOI, arXiv, OpenAlex, citation, or literature-review tasks; use `false` when academic evidence would be noise; otherwise leave `null`.
- `include_patent`: use `true` for patent, invention, applicant, assignee, inventor, IPC/CPC, publication number, or freedom-to-operate style tasks; otherwise leave `null`.
- `rerank`: leave `null` for service default. Use `false` only for low-latency exploration where ranking quality matters less.
- `top_k`: use 5-10 for direct answers, 10-20 for research or comparison tasks.

## Response Contract

The response is evidence-first:

- `evidence[]`: mixed `web`, `academic`, and `patent` evidence sorted by relevance.
- `answerability.status`: `answerable`, `partial`, or `not_answerable`.
- `answerability.gaps[]`: explicit missing evidence or quality gaps.
- `partial_failure`: true when any provider, rerank, or enrichment subtask failed.
- `failures[]`: machine-readable failure details.
- `meta.counts`: counts by evidence type.

Each evidence item has:

- `passage.text`: the usable evidence text.
- `citation`: citation-oriented metadata such as label, authors, year, DOI, work_id, or publication_number.
- `patent`: patent-only structured metadata when `type="patent"`, including publication/application numbers, applicant, inventor, IPC/CPC, country, status, family_id, dates, patent_type, and citation_count.
- `scores`: relevance, rank, rerank, authority, and confidence signals.
- `access`: openness, license, OA PDF URL, and PDF status.
- `diagnostics`: warnings, partial state, and failure code for that evidence item.

## Answering Rules

First inspect `partial_failure`, `failures[]`, and `answerability.gaps[]`.

If `answerability.status="not_answerable"`, do not present a confident final answer. Say what is missing and either run a refined search or ask the user whether to continue.

If `partial_failure=true`, use any valid evidence that was returned, but mention the failed source only when it materially affects confidence.

For academic answers, prefer `academic` evidence with DOI, venue, year, authors, and open-access status. Cite with `citation.label`, DOI, and URL when present.

For patent answers, prefer `patent` evidence and use `patent.publication_number`, `patent.applicant`, `patent.inventor`, `patent.ipc_main` or `patent.cpc_main`, and publication/application dates. Do not infer legal status beyond the returned `patent.status`.

For time-sensitive questions, check `published_date` and favor recent evidence. If dates are stale or absent, state the uncertainty.

Ground claims in `passage.text`. Do not invent sources, metadata, claims, citations, applicants, inventors, dates, licenses, or patent status that are not present in the evidence.
