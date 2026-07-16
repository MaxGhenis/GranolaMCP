# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

GranolaMCP is a Python library providing programmatic access to Granola.ai meeting data with zero external dependencies. It offers three main interfaces: Core Foundation (JSON parsing, timezone conversion), CLI Tools (5 commands), and MCP Server (8 tools for LLM integration).

## Development Commands

### Installation & Setup
```bash
# Install in development mode
pip install -e .

# Test installation
python -c "import granola_mcp; print(granola_mcp.__version__)"
```

### Running the Application
```bash
# CLI usage (after installation)
granola list --last 7d
granola show <meeting-id> --transcript
granola export <meeting-id>

# CLI usage (development)
python -m granola_mcp.cli.main list --last 7d

# MCP server
python -m granola_mcp.mcp --debug
```

### Building & Packaging
```bash
# Build package
python setup.py sdist bdist_wheel
```

## Architecture

The codebase follows a modular architecture with three main layers:

- **`granola_mcp/core/`** - Core data parsing and models
  - `parser.py` - Handles Granola's nested JSON cache structure with double parsing
  - `meeting.py` - Meeting data model with timezone conversion (UTC to CST)
  - `transcript.py` - Transcript processing and speaker identification
  - `timezone_utils.py` - UTC to CST conversion using Python's zoneinfo

- **`granola_mcp/cli/`** - Command-line interface with 5 commands
  - `commands/` - Individual CLI commands (list, show, export, stats, json)
  - `formatters/` - Output formatting with colors, tables, ASCII charts

- **`granola_mcp/mcp/`** - MCP Server for LLM integration
  - `server.py` - JSON-RPC STDIO server implementation
  - `tools.py` - 8 MCP tools for comprehensive meeting data access

## Key Technical Details

### Data Source Structure (three backends, all HTTP)

`GranolaParser` auto-selects a backend (override with `GRANOLA_MODE`). Both API
backends read Granola over HTTP — no local decryption. This matters because
Granola desktop **v7.427.0 (2026-07-16) removed `storage.dek` and encrypted
`granola.db` (SQLCipher)**, killing the old local-cache/DEK path.

- **api** (default when the CLI token bucket exists; PRIMARY, in-progress
  capable). Documents/transcripts come from the internal Granola HTTP API
  (`api.granola.ai`: `/v2/get-documents`, `/v1/get-document-transcript`,
  `/v1/get-document-panels`). The bearer token comes DEK-free from the macOS
  keychain bucket **`com.granola.cli` / `credentials`** (`{refreshToken,
  accessToken, clientId}` — a WorkOS session the desktop app keeps fresh). Read
  the token read-only in the common case; on expiry/401 it refreshes via WorkOS
  (`POST api.workos.com/user_management/authenticate`, public client, no secret)
  with a **rotation-safe** atomic keychain write-back (separate-process readback
  verify + 0600 recovery file). Required headers: `User-Agent: Granola/6
  (GranolaMCP)`, `X-Client-Version: 6.0.0`. See `core/cli_token.py` +
  `core/api_client.py`. **This is the only backend that reads just-ended /
  in-progress meetings** (segments carry `is_final`, `detected_speaker_name`,
  `source`); it surfaces a meeting ~1.5h before the public API.

- **public** (fallback; finished meetings only). The official, supported,
  versioned API (`public-api.granola.ai`: `GET /v1/notes`, `GET
  /v1/notes/{id}?include=transcript`) with a long-lived `grn_` key from
  `$GRANOLA_API_KEY` or keychain service `GRANOLA_API_KEY`. Paginates via
  `page_size` (≤30) + `has_more`/`cursor`. Read-only, no rotation, stable across
  app updates — but only returns notes that already have a generated AI summary +
  transcript (post-meeting lag; NOT in-progress). The `grn_` key is NOT valid for
  the internal `api.granola.ai` endpoints. See `core/public_api.py`.
  `get_meetings` degrades `api → public` at runtime if the internal token dies.

- **plaintext** (legacy). The original double-JSON cache parsing, used when
  `GRANOLA_CACHE_PATH` points at a readable plaintext cache file:
  ```python
  data = json.loads(open("cache-v3.json").read())
  cache = json.loads(data['cache'])
  meetings = cache['state']['documents']
  transcripts = cache['state']['transcripts']
  ```

Downstream `Meeting`/tools/CLI are backend-agnostic: `get_meetings()` returns the
same document dict shape, and `Meeting` pulls transcripts/summaries via
module-level loaders set by the parser (`set_transcript_loader` /
`set_summary_loader`).

#### Legacy decryption chain (macOS) — DEAD as of v7.427.0
`utils/granola_crypto.py` (keychain `Granola Safe Storage` → OSCrypt v10 →
`storage.dek` → 32-byte DEK → decrypt `*.enc`) no longer works: `storage.dek` is
gone and the cache DEK moved behind SQLCipher. Kept only as a fallback token
reader for `supabase.json.enc` on machines where it still exists. **Do not chase
the DEK** — it is not derivable from the 16-byte keychain secret.

### Configuration
Environment variables (or `.env`):
```env
# Backend: auto (api if CLI-token bucket present, else public if grn_ key, else
# plaintext). Override with "api" | "public" | "plaintext".
GRANOLA_MODE=api
GRANOLA_TIMEZONE=America/Chicago
GRANOLA_MAX_DOCUMENTS=500          # cap on documents/notes fetched per load
# public backend: grn_ key (env wins over keychain service GRANOLA_API_KEY)
GRANOLA_API_KEY=grn_...
GRANOLA_NOTE_CACHE_TTL=60          # public detail-cache TTL (seconds)
# api (internal) backend: com.granola.cli keychain bucket (auto); WorkOS host
GRANOLA_WORKOS_HOST=https://api.workos.com
# plaintext backend only:
GRANOLA_CACHE_PATH=~/Library/Application Support/Granola/cache-v3.json
```

### Dependencies
- **Runtime**: Python 3.12+ (`cryptography` only needed for the legacy plaintext/
  DEK fallback; both HTTP backends are stdlib-only).
- **Development**: pytest, black, flake8, mypy (optional, not currently configured)

### MCP Server Tools
The MCP server provides 8 tools: `search_meetings`, `get_meeting`, `get_transcript`, `get_meeting_notes`, `list_participants`, `get_statistics`, `export_meeting`, `analyze_patterns`. All tools support flexible date parsing (relative like `3d`, `24h` or absolute `YYYY-MM-DD`).

## Development Workflow

1. **Core Development**: Focus on `granola_mcp/core/` for data parsing and models
2. **CLI Enhancement**: Add commands in `granola_mcp/cli/commands/` and formatters in `granola_mcp/cli/formatters/`
3. **MCP Integration**: Enhance tools in `granola_mcp/mcp/tools.py`

The project always reads from live cache files (no local caching) and loads all meetings into memory on initialization.

## Development Guidelines

- All temporary and data exploration tools should go under scratch/ and you should look there for existing testing/validation tools before writing new ones.