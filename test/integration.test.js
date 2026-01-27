'use strict';

/**
 * Integration tests for shellshare.
 * 
 * Tests the actual application code (routes/rooms.js) with real MongoDB.
 */

const http = require('http');
const express = require('express');
const socketIo = require('socket.io');
const socketIoClient = require('socket.io-client');
const bodyParser = require('body-parser');
const assert = require('assert');

// Test configuration
const TEST_PORT = 3099;
const TEST_ROOM = 'test-room-' + Date.now();
const TEST_SECRET = 'test-secret-123';
const BASE_URL = `http://localhost:${TEST_PORT}`;
const MONGODB_URI = process.env.MONGODB_URI || 'mongodb://localhost:27017/shellshare_test';

// Server components
let app;
let server;
let io;

/**
 * Set up the test server using actual application code
 */
async function setup() {
  console.log('Setting up test environment...');
  console.log('MongoDB URI:', MONGODB_URI);
  
  // Connect to MongoDB using the actual db module
  const db = require('../db');
  
  await new Promise((resolve, reject) => {
    db.connect(MONGODB_URI, (err) => {
      if (err) reject(err);
      else resolve();
    });
  });
  
  console.log('Connected to MongoDB');
  
  // Create Express app similar to app.js
  app = express();
  server = http.createServer(app);
  
  app.use(bodyParser.json({ limit: '300kb' }));
  
  // Set up Socket.io
  io = socketIo.listen(server);
  
  // Use the actual rooms route
  const roomsRoute = require('../routes/rooms');
  app.use('/r', roomsRoute('/r', io));
  
  // Start server
  await new Promise((resolve) => {
    server.listen(TEST_PORT, resolve);
  });
  
  console.log(`Test server running on port ${TEST_PORT}`);
}

/**
 * Tear down the test server
 */
async function teardown() {
  console.log('Tearing down test environment...');
  
  if (io) {
    io.close();
  }
  
  if (server) {
    await new Promise((resolve) => server.close(resolve));
  }
  
  // Close MongoDB connection
  const db = require('../db');
  if (db.get()) {
    await new Promise((resolve) => db.get().close(resolve));
  }
  
  console.log('Teardown complete!');
}

/**
 * Make an HTTP request
 */
function request(method, path, body, headers = {}) {
  return new Promise((resolve, reject) => {
    const url = new URL(path, BASE_URL);
    const options = {
      hostname: url.hostname,
      port: url.port,
      path: url.pathname,
      method: method,
      headers: {
        'Content-Type': 'application/json',
        ...headers,
      },
    };

    const req = http.request(options, (res) => {
      let data = '';
      res.on('data', (chunk) => data += chunk);
      res.on('end', () => resolve({ status: res.statusCode, data }));
    });

    req.on('error', reject);
    
    if (body) {
      req.write(JSON.stringify(body));
    }
    req.end();
  });
}

/**
 * Test: POST to room should broadcast to connected viewers
 */
async function testPostBroadcastsToViewers() {
  console.log('\n--- Test: POST broadcasts to viewers ---');
  
  // Connect a viewer via Socket.io
  const client = socketIoClient(BASE_URL, {
    forceNew: true,
    transports: ['websocket'],
  });
  
  await new Promise((resolve) => client.on('connect', resolve));
  console.log('Viewer connected');
  
  // Join the room
  client.emit('join', `/r/${TEST_ROOM}`);
  
  // Wait a bit for the join to be processed
  await new Promise((resolve) => setTimeout(resolve, 100));
  
  // Set up message listener
  const receivedMessages = [];
  const receivedSizes = [];
  
  client.on('message', (msg) => receivedMessages.push(msg));
  client.on('size', (size) => receivedSizes.push(size));
  
  // POST a message to the room (this creates the authorization)
  const testMessage = 'Hello from test!';
  const testSize = { rows: 24, cols: 80 };
  
  const response = await request('POST', `/r/${TEST_ROOM}`, {
    message: testMessage,
    size: testSize,
  }, {
    'Authorization': TEST_SECRET,
  });
  
  console.log('POST response status:', response.status);
  assert.strictEqual(response.status, 200, 'POST should return 200');
  
  // Wait for Socket.io messages
  await new Promise((resolve) => setTimeout(resolve, 200));
  
  // Verify the viewer received the message
  console.log('Received messages:', receivedMessages);
  console.log('Received sizes:', receivedSizes);
  
  assert.ok(receivedMessages.includes(testMessage), 'Viewer should receive the message');
  assert.ok(receivedSizes.some(s => s.rows === 24 && s.cols === 80), 'Viewer should receive the size');
  
  client.disconnect();
  console.log('✓ Test passed: POST broadcasts to viewers');
}

/**
 * Test: Unauthorized POST should fail
 */
async function testUnauthorizedPostFails() {
  console.log('\n--- Test: Unauthorized POST fails ---');
  
  // First, create the room with one secret
  await request('POST', `/r/${TEST_ROOM}-auth`, {
    message: 'initial',
    size: { rows: 24, cols: 80 },
  }, {
    'Authorization': 'correct-secret',
  });
  
  // Try to POST with a different secret
  const response = await request('POST', `/r/${TEST_ROOM}-auth`, {
    message: 'unauthorized',
    size: { rows: 24, cols: 80 },
  }, {
    'Authorization': 'wrong-secret',
  });
  
  console.log('Unauthorized POST response status:', response.status);
  assert.strictEqual(response.status, 401, 'Unauthorized POST should return 401');
  
  console.log('✓ Test passed: Unauthorized POST fails');
}

/**
 * Test: DELETE room
 */
async function testDeleteRoom() {
  console.log('\n--- Test: DELETE room ---');
  
  const roomName = `${TEST_ROOM}-delete`;
  const secret = 'delete-secret';
  
  // Create the room
  await request('POST', `/r/${roomName}`, {
    message: 'to be deleted',
    size: { rows: 24, cols: 80 },
  }, {
    'Authorization': secret,
  });
  
  // Delete the room
  const response = await request('DELETE', `/r/${roomName}`, null, {
    'Authorization': secret,
  });
  
  console.log('DELETE response status:', response.status);
  assert.strictEqual(response.status, 202, 'DELETE should return 202');
  
  console.log('✓ Test passed: DELETE room');
}

/**
 * Test: New viewer receives existing room content
 */
async function testViewerReceivesExistingContent() {
  console.log('\n--- Test: Viewer receives existing content ---');
  
  const roomName = `${TEST_ROOM}-existing`;
  const testMessage = 'Existing content';
  const testSize = { rows: 30, cols: 100 };
  
  // First, POST content to the room
  await request('POST', `/r/${roomName}`, {
    message: testMessage,
    size: testSize,
  }, {
    'Authorization': 'existing-secret',
  });
  
  // Now connect a viewer
  const client = socketIoClient(BASE_URL, {
    forceNew: true,
    transports: ['websocket'],
  });
  
  await new Promise((resolve) => client.on('connect', resolve));
  
  // Set up listeners before joining
  const receivedMessages = [];
  const receivedSizes = [];
  
  client.on('message', (msg) => receivedMessages.push(msg));
  client.on('size', (size) => receivedSizes.push(size));
  
  // Join the room
  client.emit('join', `/r/${roomName}`);
  
  // Wait for room data
  await new Promise((resolve) => setTimeout(resolve, 300));
  
  console.log('Received messages:', receivedMessages);
  console.log('Received sizes:', receivedSizes);
  
  // Viewer should receive the existing content
  assert.ok(receivedMessages.includes(testMessage), 'Viewer should receive existing message');
  assert.ok(receivedSizes.some(s => s.rows === 30 && s.cols === 100), 'Viewer should receive existing size');
  
  client.disconnect();
  console.log('✓ Test passed: Viewer receives existing content');
}

/**
 * Run all tests
 */
async function runTests() {
  try {
    await setup();
    
    await testPostBroadcastsToViewers();
    await testUnauthorizedPostFails();
    await testDeleteRoom();
    await testViewerReceivesExistingContent();
    
    console.log('\n========================================');
    console.log('All tests passed! ✓');
    console.log('========================================\n');
    
    await teardown();
    process.exit(0);
  } catch (error) {
    console.error('\n❌ Test failed:', error.message);
    console.error(error);
    await teardown();
    process.exit(1);
  }
}

runTests();
