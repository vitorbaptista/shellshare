# E2E Black-Box Tests

These tests treat Shellshare as a black box. They interact only through public interfaces:
- **HTTP API** (same as the Python CLI)
- **Browser** (headless Chrome via Playwright)

## Why Black-Box?

These tests will continue to work even if:
- The implementation language changes (Node → Elixir, Go, etc.)
- Internal architecture changes
- Database changes
- Only the API contract and browser behavior need to stay the same

## What's Tested

1. **Happy Path**: Broadcast message → appears in browser
2. **Multiple Messages**: Messages arrive in order
3. **Replay**: New viewers see existing content

## Running Locally

1. Start MongoDB:
   ```bash
   docker run -d -p 27017:27017 mongo:5.0
   ```

2. Start the server:
   ```bash
   npm install
   npm start
   ```

3. Run tests:
   ```bash
   cd e2e
   npm install
   npx playwright install chromium
   npx playwright test
   ```

## CI

Tests run automatically on GitHub Actions with:
- MongoDB service container
- Headless Chromium browser
- Server started in background
