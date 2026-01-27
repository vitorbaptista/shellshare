'use strict';

/**
 * Integration tests for shellshare happy path.
 *
 * Tests the complete flow:
 * 1. Create a room by posting a message
 * 2. Connect a viewer via Socket.io
 * 3. Verify the viewer receives the broadcasted message
 * 4. Clean up the room
 */

const http = require('http');
const express = require('express');
const socketIo = require('socket.io');
const socketIoClient = require('socket.io-client');
const bodyParser = require('body-parser');
const { MongoMemoryServer } = require('mongodb-memory-server');
const { MongoClient } = require('mongodb');
const assert = require('assert');

// Test configuration
const TEST_PORT = 3001;
const TEST_ROOM = 'test-room-' + Date.now();
const TEST_SECRET = 'test-secret-123';
const BASE_URL = `http://localhost:${TEST_PORT}`;

// Server components
let mongoServer;
let mongoClient;
let app;
let server;
let io;

/**
 * Create the Express app with routes (simplified version of app.js)
 */
function createApp(db) {
  const app = express();
  app.use(bodyParser.json({ limit: '300kb' }));

  // Room route handlers
  const roomsState = new Map(); // In-memory store for simplicity

  app.post('/r/:room', (req, res) => {
    const room = req.params.room;
    const secret = req.get('Authorization');
    const size = req.body.size;
    const message = req.body.message;

    // Simple authorization: first poster owns the room
    if (!roomsState.has(room)) {
      roomsState.set(room, { secret, messages: [] });
    } else if (roomsState.get(room).secret !== secret) {
      return res.sendStatus(401);
    }

    // Store message
    const roomData = roomsState.get(room);
    roomData.messages.push({ size, message });
    roomData.size = size;

    // Broadcast to viewers
    io.sockets.in(room).emit('size', size);
    io.sockets.in(room).emit('message', message);

    res.sendStatus(200);
  });

  app.delete('/r/:room', (req, res) => {
    const room = req.params.room;
    const secret = req.get('Authorization');

    if (roomsState.has(room) && roomsState.get(room).secret === secret) {
      roomsState.delete(room);
      res.sendStatus(202);
    } else {
      res.sendStatus(401);
    }
  });

  app.get('/r/:room', (req, res) => {
    res.send('Room viewer page');
  });

  return { app, roomsState };
}

/**
 * Setup Socket.io with room joining logic
 */
function setupSocketIo(ioInstance, roomsState) {
  ioInstance.on('connection', (socket) => {
    socket.on('join', (room) => {
      // Strip /r/ prefix if present
      if (room.startsWith('/r/')) {
        room = room.slice(3);
      }

      socket.join(room, (err) => {
        if (!err) {
          // Send current room state to new viewer
          const roomData = roomsState.get(room);
          if (roomData) {
            socket.emit('size', roomData.size);
            // Send all accumulated messages
            const allMessages = roomData.messages.map(m => m.message).join('');
            if (allMessages) {
              socket.emit('message', allMessages);
            }
          }

          // Update user count
          const clients = ioInstance.sockets.adapter.rooms[room];
          if (clients) {
            ioInstance.in(room).emit('usersCount', Object.keys(clients.sockets || clients).length);
          }
        }
      });
    });
  });
}

/**
 * Helper to make HTTP requests
 */
function makeRequest(method, path, data, headers = {}) {
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
      let body = '';
      res.on('data', (chunk) => body += chunk);
      res.on('end', () => resolve({ status: res.statusCode, body }));
    });

    req.on('error', reject);

    if (data) {
      req.write(JSON.stringify(data));
    }
    req.end();
  });
}

/**
 * Helper to encode message like the Python client does
 */
function encodeMessage(text) {
  const urlEncoded = encodeURIComponent(text);
  return Buffer.from(urlEncoded).toString('base64');
}

/**
 * Helper to decode message like the browser does
 */
function decodeMessage(encoded) {
  return decodeURIComponent(Buffer.from(encoded, 'base64').toString('utf-8'));
}

// ============================================================================
// Test Setup and Teardown
// ============================================================================

async function setup() {
  console.log('Setting up test environment...');

  // Start in-memory MongoDB
  mongoServer = await MongoMemoryServer.create();
  const mongoUri = mongoServer.getUri();
  mongoClient = await MongoClient.connect(mongoUri, { useUnifiedTopology: true });

  console.log(`  MongoDB started at ${mongoUri}`);

  // Create Express app
  const { app: expressApp, roomsState } = createApp(mongoClient.db());
  app = expressApp;

  // Create HTTP server and Socket.io
  server = http.createServer(app);
  io = socketIo(server);

  // Setup Socket.io handlers
  setupSocketIo(io, roomsState);

  // Start listening
  await new Promise((resolve) => {
    server.listen(TEST_PORT, () => {
      console.log(`  Server started at ${BASE_URL}`);
      resolve();
    });
  });

  console.log('Setup complete!\n');
}

async function teardown() {
  console.log('\nTearing down test environment...');

  if (io) {
    io.close();
  }

  if (server) {
    await new Promise((resolve) => server.close(resolve));
    console.log('  Server stopped');
  }

  if (mongoClient) {
    await mongoClient.close();
  }

  if (mongoServer) {
    await mongoServer.stop();
    console.log('  MongoDB stopped');
  }

  console.log('Teardown complete!');
}

// ============================================================================
// Tests
// ============================================================================

async function testHappyPath() {
  console.log('=== TEST: Happy Path - Create Room, Stream, Broadcast ===\n');

  const testMessage = 'Hello, shellshare!';
  const encodedMessage = encodeMessage(testMessage);
  const terminalSize = { cols: 80, rows: 24 };

  // Step 1: Connect a viewer via Socket.io
  console.log('1. Connecting viewer via Socket.io...');
  const viewer = socketIoClient(BASE_URL);

  const receivedMessages = [];
  let receivedSize = null;
  let userCount = 0;

  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('Viewer connection timeout')), 5000);

    viewer.on('connect', () => {
      console.log('   Viewer connected');
      clearTimeout(timeout);
      resolve();
    });

    viewer.on('error', reject);
  });

  // Setup message listeners
  viewer.on('message', (msg) => {
    console.log(`   Viewer received message: ${decodeMessage(msg).substring(0, 50)}...`);
    receivedMessages.push(msg);
  });

  viewer.on('size', (size) => {
    console.log(`   Viewer received size: ${size.cols}x${size.rows}`);
    receivedSize = size;
  });

  viewer.on('usersCount', (count) => {
    console.log(`   User count updated: ${count}`);
    userCount = count;
  });

  // Join the room
  console.log(`   Joining room: ${TEST_ROOM}`);
  viewer.emit('join', `/r/${TEST_ROOM}`);

  // Give it a moment to join
  await new Promise((resolve) => setTimeout(resolve, 100));

  // Step 2: Post a message to the room (simulating broadcaster)
  console.log('\n2. Broadcasting message to room...');
  const postResponse = await makeRequest(
    'POST',
    `/r/${TEST_ROOM}`,
    { message: encodedMessage, size: terminalSize },
    { 'Authorization': TEST_SECRET }
  );

  console.log(`   POST response status: ${postResponse.status}`);
  assert.strictEqual(postResponse.status, 200, 'POST should return 200');

  // Wait for message to be broadcast
  await new Promise((resolve) => setTimeout(resolve, 200));

  // Step 3: Verify viewer received the message
  console.log('\n3. Verifying viewer received broadcast...');

  assert.ok(receivedMessages.length > 0, 'Viewer should have received at least one message');
  const decodedReceived = decodeMessage(receivedMessages[0]);
  assert.strictEqual(decodedReceived, testMessage, 'Received message should match sent message');
  console.log('   ✓ Message received correctly');

  assert.ok(receivedSize, 'Viewer should have received size');
  assert.strictEqual(receivedSize.cols, terminalSize.cols, 'Cols should match');
  assert.strictEqual(receivedSize.rows, terminalSize.rows, 'Rows should match');
  console.log('   ✓ Terminal size received correctly');

  // Step 4: Test authorization (different secret should fail)
  console.log('\n4. Testing authorization...');
  const unauthorizedResponse = await makeRequest(
    'POST',
    `/r/${TEST_ROOM}`,
    { message: encodedMessage, size: terminalSize },
    { 'Authorization': 'wrong-secret' }
  );

  assert.strictEqual(unauthorizedResponse.status, 401, 'Wrong secret should return 401');
  console.log('   ✓ Unauthorized request correctly rejected');

  // Step 5: Clean up - delete the room
  console.log('\n5. Cleaning up room...');
  const deleteResponse = await makeRequest(
    'DELETE',
    `/r/${TEST_ROOM}`,
    null,
    { 'Authorization': TEST_SECRET }
  );

  assert.strictEqual(deleteResponse.status, 202, 'DELETE should return 202');
  console.log('   ✓ Room deleted successfully');

  // Disconnect viewer
  viewer.disconnect();
  console.log('   ✓ Viewer disconnected');

  console.log('\n=== TEST PASSED ===\n');
}

async function testMultipleMessages() {
  console.log('=== TEST: Multiple Messages Streaming ===\n');

  const room = 'multi-msg-' + Date.now();
  const messages = ['First line\n', 'Second line\n', 'Third line\n'];

  // Connect viewer
  const viewer = socketIoClient(BASE_URL);
  const receivedMessages = [];

  await new Promise((resolve) => {
    viewer.on('connect', resolve);
  });

  viewer.on('message', (msg) => {
    receivedMessages.push(decodeMessage(msg));
  });

  viewer.emit('join', `/r/${room}`);
  await new Promise((resolve) => setTimeout(resolve, 100));

  // Send multiple messages
  console.log('1. Sending multiple messages...');
  for (const msg of messages) {
    await makeRequest(
      'POST',
      `/r/${room}`,
      { message: encodeMessage(msg), size: { cols: 80, rows: 24 } },
      { 'Authorization': TEST_SECRET }
    );
    await new Promise((resolve) => setTimeout(resolve, 50));
  }

  await new Promise((resolve) => setTimeout(resolve, 200));

  // Verify
  console.log('2. Verifying all messages received...');
  assert.strictEqual(receivedMessages.length, messages.length, 'Should receive all messages');
  for (let i = 0; i < messages.length; i++) {
    assert.strictEqual(receivedMessages[i], messages[i], `Message ${i} should match`);
  }
  console.log('   ✓ All messages received in order');

  // Cleanup
  await makeRequest('DELETE', `/r/${room}`, null, { 'Authorization': TEST_SECRET });
  viewer.disconnect();

  console.log('\n=== TEST PASSED ===\n');
}

async function testLateJoiner() {
  console.log('=== TEST: Late Joiner Receives History ===\n');

  const room = 'late-join-' + Date.now();
  const message = 'Message before viewer joins';

  // Send message first (before viewer connects)
  console.log('1. Sending message before viewer joins...');
  await makeRequest(
    'POST',
    `/r/${room}`,
    { message: encodeMessage(message), size: { cols: 80, rows: 24 } },
    { 'Authorization': TEST_SECRET }
  );

  // Now connect viewer
  console.log('2. Connecting late viewer...');
  const viewer = socketIoClient(BASE_URL);
  const receivedMessages = [];

  await new Promise((resolve) => {
    viewer.on('connect', resolve);
  });

  viewer.on('message', (msg) => {
    receivedMessages.push(decodeMessage(msg));
  });

  viewer.emit('join', `/r/${room}`);
  await new Promise((resolve) => setTimeout(resolve, 200));

  // Verify late joiner received the message
  console.log('3. Verifying late joiner received history...');
  assert.ok(receivedMessages.length > 0, 'Late joiner should receive message history');
  assert.ok(receivedMessages[0].includes(message), 'History should contain the message');
  console.log('   ✓ Late joiner received message history');

  // Cleanup
  await makeRequest('DELETE', `/r/${room}`, null, { 'Authorization': TEST_SECRET });
  viewer.disconnect();

  console.log('\n=== TEST PASSED ===\n');
}

// ============================================================================
// Main
// ============================================================================

async function runTests() {
  try {
    await setup();

    await testHappyPath();
    await testMultipleMessages();
    await testLateJoiner();

    console.log('\n✅ All tests passed!\n');
  } catch (error) {
    console.error('\n❌ Test failed:', error.message);
    console.error(error.stack);
    process.exitCode = 1;
  } finally {
    await teardown();
  }
}

runTests();
