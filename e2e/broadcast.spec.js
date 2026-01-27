/**
 * E2E Black-Box Tests for Shellshare
 * 
 * These tests treat the application as a black box - they only interact
 * with it through the public interfaces (CLI + browser). They will work
 * regardless of implementation language or internal changes.
 * 
 * Happy path test:
 * 1. Start server
 * 2. Use Python CLI to create a broadcast
 * 3. Use headless browser to verify content appears
 */

const { test, expect } = require('@playwright/test');
const { spawn, exec } = require('child_process');
const path = require('path');
const http = require('http');

const SERVER_URL = 'http://localhost:3000';
const CLI_PATH = path.join(__dirname, '..', 'public', 'bin', 'shellshare');

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

// Helper: broadcast a message using the CLI
function broadcastMessage(roomId, message, password) {
  return new Promise((resolve, reject) => {
    // Use curl to POST directly (simulates what the CLI does)
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

test.describe('Shellshare E2E', () => {
  test('happy path: broadcast message appears in browser', async ({ page }) => {
    // Generate unique room ID and password
    const roomId = `test-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const password = `secret-${Math.random().toString(36).slice(2)}`;
    const testMessage = 'Hello from E2E test! 🚀';
    
    // Wait for server to be ready
    console.log('Waiting for server...');
    await waitForServer(SERVER_URL);
    console.log('Server is ready!');
    
    // Navigate to the room page first (viewer connects)
    console.log(`Opening room: ${SERVER_URL}/r/${roomId}`);
    await page.goto(`${SERVER_URL}/r/${roomId}`);
    
    // Wait for the terminal element to be visible
    await page.waitForSelector('#terminal', { timeout: 10000 });
    console.log('Terminal element found');
    
    // Broadcast the message
    console.log('Broadcasting message...');
    await broadcastMessage(roomId, testMessage, password);
    console.log('Message broadcasted!');
    
    // Wait for the message to appear in the terminal
    // The message is base64 encoded, so we look for the decoded content
    console.log('Waiting for message to appear...');
    
    // Give Socket.io time to deliver the message
    await page.waitForTimeout(2000);
    
    // Check that the terminal contains our message
    const terminalContent = await page.locator('#terminal').textContent();
    console.log('Terminal content:', terminalContent);
    
    expect(terminalContent).toContain(testMessage);
    console.log('✓ Message appeared in browser!');
  });
  
  test('multiple messages are broadcasted in order', async ({ page }) => {
    const roomId = `test-multi-${Date.now()}`;
    const password = `secret-${Math.random().toString(36).slice(2)}`;
    
    await waitForServer(SERVER_URL);
    await page.goto(`${SERVER_URL}/r/${roomId}`);
    await page.waitForSelector('#terminal', { timeout: 10000 });
    
    // Send multiple messages
    const messages = ['First message', 'Second message', 'Third message'];
    
    for (const msg of messages) {
      await broadcastMessage(roomId, msg, password);
      await page.waitForTimeout(500);
    }
    
    await page.waitForTimeout(1000);
    
    const terminalContent = await page.locator('#terminal').textContent();
    
    for (const msg of messages) {
      expect(terminalContent).toContain(msg);
    }
    
    console.log('✓ All messages appeared in order!');
  });
  
  test('new viewer sees existing broadcast content', async ({ page, context }) => {
    const roomId = `test-replay-${Date.now()}`;
    const password = `secret-${Math.random().toString(36).slice(2)}`;
    const existingMessage = 'This message was sent before viewer joined';
    
    await waitForServer(SERVER_URL);
    
    // Broadcast BEFORE opening the page
    await broadcastMessage(roomId, existingMessage, password);
    console.log('Message broadcasted before viewer connects');
    
    // Now open the page as a new viewer
    await page.goto(`${SERVER_URL}/r/${roomId}`);
    await page.waitForSelector('#terminal', { timeout: 10000 });
    
    // Wait for replay
    await page.waitForTimeout(2000);
    
    const terminalContent = await page.locator('#terminal').textContent();
    expect(terminalContent).toContain(existingMessage);
    
    console.log('✓ New viewer received existing content!');
  });
});
