/**
 * E2E Black-Box Tests for Shellshare
 * 
 * These tests treat the application as a black box - they only interact
 * with it through the public interfaces (CLI + browser). They will work
 * regardless of implementation language or internal changes.
 */

const { test, expect } = require('@playwright/test');
const { spawn } = require('child_process');
const http = require('http');

const SERVER_URL = 'http://localhost:3000';

// Helper: wait for server to be ready
async function waitForServer(url, timeoutMs = 30000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      await new Promise((resolve, reject) => {
        const req = http.get(url, (res) => {
          resolve(res.statusCode);
        });
        req.on('error', reject);
        req.setTimeout(1000, () => {
          req.destroy();
          reject(new Error('timeout'));
        });
      });
      return true;
    } catch (e) {
      await new Promise(r => setTimeout(r, 500));
    }
  }
  throw new Error(`Server not ready after ${timeoutMs}ms`);
}

// Helper: broadcast a message using curl (same as CLI does)
function broadcastMessage(roomId, message, password) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify({
      message: Buffer.from(encodeURIComponent(message)).toString('base64'),
      size: { rows: 24, cols: 80 }
    });
    
    const curl = spawn('curl', [
      '-s',
      '-X', 'POST',
      '-H', 'Content-Type: application/json',
      '-H', `Authorization: ${password}`,
      '-d', data,
      `${SERVER_URL}/r/${roomId}`
    ]);
    
    let stdout = '';
    let stderr = '';
    
    curl.stdout.on('data', (d) => stdout += d);
    curl.stderr.on('data', (d) => stderr += d);
    
    curl.on('close', (code) => {
      if (code === 0) {
        resolve(stdout);
      } else {
        reject(new Error(`curl failed: ${stderr}`));
      }
    });
  });
}

// Helper: normalize text for comparison (trim whitespace)
function normalize(text) {
  return text.replace(/\s+/g, ' ').trim();
}

test.describe('Shellshare E2E', () => {
  test('happy path: broadcast message appears in browser', async ({ page }) => {
    const roomId = `test-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const password = `secret-${Math.random().toString(36).slice(2)}`;
    const testMessage = 'Hello from E2E test';  // No emoji for reliability
    
    console.log('Waiting for server...');
    await waitForServer(SERVER_URL);
    console.log('Server ready!');
    
    // Navigate to room
    console.log(`Opening room: ${SERVER_URL}/r/${roomId}`);
    await page.goto(`${SERVER_URL}/r/${roomId}`);
    await page.waitForSelector('#terminal', { timeout: 10000 });
    console.log('Terminal found');
    
    // Broadcast the message
    console.log('Broadcasting:', testMessage);
    await broadcastMessage(roomId, testMessage, password);
    
    // Wait and verify
    await page.waitForTimeout(2000);
    
    const terminalContent = await page.locator('#terminal').textContent();
    const normalizedContent = normalize(terminalContent);
    console.log('Terminal content (normalized):', normalizedContent);
    
    // Use includes() for flexible matching
    const found = normalizedContent.includes(testMessage);
    console.log('Message found:', found);
    
    expect(found).toBe(true);
    console.log('PASSED: Message appeared in browser');
  });
});
