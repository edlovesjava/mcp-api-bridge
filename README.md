# mcp-api-bridge

An MCP server that puts your existing in-house catalog search APIs in front of
any MCP client, with Amazon Bedrock doing the natural-language → structured
query translation.

Two ideas carry the whole thing:

1. **Catalogs are configuration, not code.** A YAML file describes each API's
   URL, auth, request shape, and response shape. Adding a catalog means adding
   a config block.
2. **The filter vocabulary is the prompt.** The `description`, `type`, and
   `values` you write for each filter are handed to Bedrock verbatim as the
   vocabulary it maps language onto. Describe a filter well and query
   understanding works for it — there is no separate prompt to maintain.

```
MCP client ──▶ mcp-api-bridge ──▶ Bedrock (Claude)   plan the query
                     │
                     └──────────▶ your catalog REST API   execute the search
```

## Tools exposed

| Tool | What it does |
|---|---|
| `list_catalogs` | Describes every configured catalog: its filters, allowed values, and sorts. Call this first to learn the vocabulary. |
| `catalog_search` | Runs a search with explicit `filters` / `sort` / paging. No model in the loop. |
| `catalog_get_item` | Fetches one item by id, for catalogs that configure a `get_item` endpoint. |
| `understand_query` | Bedrock turns a natural-language query into a `QueryPlan` — keywords, filters, sort, synonyms — without searching. |
| `smart_search` | `understand_query` then `catalog_search`, returning both the plan and the results. |

`catalog_search` is deliberately usable on its own: when the client is already
an LLM that knows the filter vocabulary from `list_catalogs`, the Bedrock hop
is redundant latency. Reach for `smart_search` when a raw end-user string
needs interpreting.

## Quick start

```bash
uv venv && uv pip install -e ".[dev]"

cp config/catalog.example.yaml config/catalog.yaml   # then edit
cp .env.example .env                                 # then fill in

export MCP_API_BRIDGE_CONFIG=./config/catalog.yaml
export CATALOG_TOKEN=...        # whatever your config's auth blocks name
export AWS_REGION=us-east-1     # plus standard AWS credentials

.venv/bin/mcp-api-bridge        # speaks MCP over stdio
```

Register it with an MCP client:

```json
{
  "mcpServers": {
    "catalog": {
      "command": "/path/to/mcp-api-bridge/.venv/bin/mcp-api-bridge",
      "env": {
        "MCP_API_BRIDGE_CONFIG": "/path/to/config/catalog.yaml",
        "CATALOG_TOKEN": "...",
        "AWS_REGION": "us-east-1"
      }
    }
  }
}
```

## Importing from OpenAPI

Hand-writing config stops being viable fast: the bundled
`catalog-search-service-api.json` declares **49 query parameters** on one
operation across three sibling endpoints. `import-openapi` reads the spec and
emits config, so the wire contract comes from the API team's own document.

```bash
# See what's in the spec
mcp-api-bridge import-openapi catalog-search-service-api.json --list

# Generate, curating the filters that reach the model
mcp-api-bridge import-openapi catalog-search-service-api.json \
  --operation productions=searchProductions \
  --include productions=startDate,endDate,city,stateCode,months,minListingPriceFloor \
  --operation performers=getPerformers \
  --include performers=activeFilter,minProductionCount \
  -o config/catalog.yaml
```

`config/vividseats.example.yaml` was scaffolded this way, then hand-edited for
the parts below that no spec can supply.

It takes from the spec: base URL, method, path, parameter names, types, enums,
descriptions, paging defaults, the response envelope, and the auth scheme. It
strips HTML out of descriptions first — 81 of the 150 parameter descriptions in
that spec contain `<br/>` or `<b>`, and those strings become the model's
vocabulary.

It cannot take the **semantic roles**, because OpenAPI does not carry them:
which parameter is the free-text query, which response field is the title,
when each sort applies. Those are guessed by name and every guess is reported —
to stderr as it runs, and as a comment block at the top of the generated file:

```
note: roles matched by name: query=query, page=page, page_size=pageSize, sortBy=sort — verify
note: first_page=1 taken from the 'page' default
TODO: base_url ...vividseats-staging.com looks non-production — confirm before deploying
TODO: sort descriptions are blank — the spec has only raw enum values
```

**`--include` matters more than it looks.** Every filter enters the
query-understanding prompt, so importing all 45 makes the prompt expensive and
gives the model a wide surface to invent against. The importer nags when an
uncurated import exceeds 15 filters.

**One thing the spec cannot fix.** That spec declares no `securitySchemes` at
all, so the importer writes `auth: none`; if a gateway fronts the API, that is
invisible here. (The opaque-id problem it also surfaces is handled — see the
next section.)

The importer needs no dependencies beyond PyYAML — `$ref`s are resolved on
demand with cycle guards, because real specs are cyclic (`Production` → `Venue`
→ `Production`).

## Opaque ids: names in, ids out

The best filters on a real catalog are keyed by ids — `regionId`, `performerId`,
`venueId`, `categoryId`. No model turns *"Taylor Swift in Chicago"* into
`performerId=9134&regionId=5` from a description, so a filter like that is dead
weight in the vocabulary. Declaring how a filter's values resolve brings it
back to life. Two strategies, because there are two shapes of id:

**`lookup` — closed sets** (regions, categories). The bridge fetches the table
once, publishes the *names* as the filter's vocabulary, and translates back to
the id on the way out. This is not an optimization: `GET /v1/regions` filters
by IP and lat/long only, so holding the list is the *only* way to match
"Chicago".

```yaml
region:
  param: regionId
  type: integer
  description: Metro area the event's venue sits in.
  lookup:
    path: /v1/regions
    id_field: id
    name_field: name
    alias_fields: [listName]     # accept "Chicago, IL" too
```

**`resolve` — open sets** (performers, venues). No table can be preloaded, so
the model supplies the name it read and the bridge searches a sibling catalog
for it. Pointing this at the Algolia-backed search service is deliberate — it
handles misspellings and partial names, which is exactly what resolution needs.

```yaml
performer:
  param: performerId
  type: integer
  description: Artist, team, or touring show.
  resolve:
    api: performers              # another configured catalog
```

Either way the caller passes a name and gets told what it became:

```json
"resolutions": {
  "performer": {"name": "Taylor Swift", "id": "9134"},
  "region":    {"name": "Chicago",      "id": "5"}
}
```

That readback matters because resolution is a guess where names collide —
"Chicago" is a city, a band, and a musical. `list_catalogs` marks these filters
`accepts_name`, listing `allowed_values` for closed sets and leaving it null for
open ones. An id passed directly still works and skips the lookup entirely; an
unrecognised name gets a did-you-mean.

`config/vividseats.example.yaml` wires both services together this way:
catalog-service for authoritative data and the reference tables,
catalog-search-service for name resolution.

## Configuring a catalog

`config/catalog.example.yaml` is a commented walkthrough of both common
shapes: a GET search API with a nested response envelope, and a POST search
API with body filters and zero-indexed paging. Generated config uses the same
format, so anything below applies to imported catalogs too. The pieces:

**Request templating.** Any value under `query:` or `body:` may contain
`{query}`, `{page}`, `{page_size}`, or `{sort}`. A value that is exactly one
placeholder keeps its type (`size: "{page_size}"` sends an integer). A
placeholder with nothing to fill it drops the whole parameter — that is how
optional params disappear when the caller omits them. Values with no
placeholder are sent as-is, which covers static params like `channel: web`.

**Response mapping.** `items_path` and `total_path` locate the result list and
the count inside whatever envelope the API uses. Paths are dotted with
optional indices — `data.items`, `media[0].url`, `variants[*].sku`. `fields`
maps the five normalized fields (`id`, `title`, `description`, `url`, `image`);
`attributes` carries anything else through untouched. With no `fields` mapping
at all, the raw upstream item passes through as attributes.

**Auth.** `none`, `bearer`, `api_key` (header or query param), or `basic`.
Every variant names an environment variable; no credential is ever read from
the config file.

**Paging.** `first_page: 0` for APIs that page from zero. Callers always pass
1-based `page`; the bridge translates.

## The Bedrock layer

`understand_query` constrains Claude to the `QueryPlan` schema via structured
outputs, so the response is always parseable — no regex over prose, no retry
loop for malformed JSON. The plan carries:

- `keywords` — search terms with filter-like phrases removed, so the keyword
  match is not fighting the filters
- `filters` — only names the catalog actually exposes
- `sort` — only sorts the catalog actually exposes
- `expansions` — domain synonyms to retry with if results are thin
- `intent` and `ambiguities` — for the trace, and for a client deciding
  whether to ask a clarifying question

Filters and sorts the model invents anyway are dropped before they reach the
catalog, so a hallucination degrades to a slightly broader search rather than
a failed request.

Defaults are `anthropic.claude-opus-5` at `effort: low` — query understanding
is a short hop in front of the real work, and low effort keeps it cheap
without changing the answers on typical queries. Override per deployment:

```yaml
bedrock:
  model_id: anthropic.claude-opus-5
  effort: medium
  guidance: |
    Domain shorthand the model should know about.
```

or with `$BEDROCK_MODEL_ID` / `$BEDROCK_EFFORT` / `$BEDROCK_REGION`.

Bedrock is only ever on the `understand_query` and `smart_search` paths. If it
is unreachable, `catalog_search`, `catalog_get_item`, and `list_catalogs` keep
working — and `smart_search` says so in its error rather than failing silently.

## Tests

```bash
.venv/bin/pytest
```

The suite mocks the catalog HTTP layer with `respx` and stubs the Bedrock
client, so it runs with no network and no AWS credentials. The OpenAPI tests
run against the real `catalog-search-service-api.json` rather than a tidy
fixture — its cyclic `$ref`s, HTML descriptions, 3.1 type unions, and missing
security scheme each broke a first draft of the importer.
