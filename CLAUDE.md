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

### Data Source Structure (two backends)

`GranolaParser` auto-selects a backend (override with `GRANOLA_MODE`):

- **api** (default, modern Granola 2026+). Granola now encrypts all local
  storage and no longer keeps documents in the cache (only recent transcripts).
  So documents come from the Granola HTTP API (`api.granola.ai`) using the
  app's bearer token, which is read from the encrypted `supabase.json.enc`.
  Transcripts come from the decrypted local cache (instant) with a lazy API
  fallback (`/v1/get-document-transcript`); AI summaries come lazily from
  panels (`/v1/get-document-panels`). See `core/api_client.py` and
  `utils/granola_crypto.py`. It is **read-only** — it never refreshes the
  token, so the desktop app's session is never disturbed.

- **plaintext** (legacy). The original double-JSON cache parsing, used when
  `GRANOLA_CACHE_PATH` points at a readable plaintext cache file:
  ```python
  data = json.loads(open("cache-v3.json").read())
  cache = json.loads(data['cache'])
  meetings = cache['state']['documents']
  transcripts = cache['state']['transcripts']
  ```

#### Decryption chain (macOS, api backend)
`security` reads the `Granola Safe Storage` / `Granola Key` keychain item ONCE
(targeted, not a dump → no prompt storms; "Always Allow" makes it silent) →
derives the Chromium OSCrypt v10 key (PBKDF2-SHA1, salt `saltysalt`) → decrypts
`storage.dek` to the 32-byte DEK → decrypts `*.enc` (AES-256-GCM, nonce(12) ||
ct || tag(16), optional gzip). Downstream `Meeting`/tools/CLI are unchanged:
`get_meetings()` still returns the same document dict shape, and `Meeting`
pulls transcripts/summaries via module-level loaders set by the parser
(`set_transcript_loader` / `set_summary_loader`).

### Configuration
Environment variables (or `.env`):
```env
# Backend: "api" (default, modern Granola) or "plaintext" (legacy cache file)
GRANOLA_MODE=api
# Only for the plaintext backend — path to a decrypted/legacy cache file
GRANOLA_CACHE_PATH=~/Library/Application Support/Granola/cache-v3.json
GRANOLA_TIMEZONE=America/Chicago
# api backend tuning
GRANOLA_MAX_DOCUMENTS=500          # cap on documents fetched per load
GRANOLA_DIR=~/Library/Application Support/Granola   # override Granola data dir
GRANOLA_KEYCHAIN_SERVICE="Granola Safe Storage"      # safeStorage keychain item
GRANOLA_KEYCHAIN_ACCOUNT="Granola Key"
```

### Dependencies
- **Runtime**: Python 3.12+ and `cryptography` (for the api backend's local decryption)
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