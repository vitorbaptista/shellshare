# E2E Black-Box Tests

These tests treat Shellshare as a black box. They interact only through public interfaces:
- **HTTP API** (same protocol as the Python CLI)
- **Browser** (headless Chromium via Playwright)

## Why Black-Box?

These tests will continue to work even if:
- The implementation language changes (Node → Elixir, Go, etc.)
- Internal architecture changes
- Database changes
- Only the API contract and browser behavior need to stay the same

## What's Tested

- **Happy Path**: Broadcast message → appears in browser

## Running Locally

1. Install [uv](https://docs.astral.sh/uv/):
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. Start MongoDB:
   ```bash
   docker run -d -p 27017:27017 mongo:5.0
   ```

3. Start the server:
   ```bash
   npm install
   npm start
   ```

4. Run tests:
   ```bash
   cd e2e
   uv sync
   uv run playwright install chromium
   uv run pytest -v
   ```

## CI

Tests run automatically on GitHub Actions with:
- MongoDB service container
- Node.js 14 for the server
- Python + UV for the tests
- Headless Chromium browser
