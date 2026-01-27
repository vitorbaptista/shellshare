# Integration Tests

Tests for the shellshare application using the actual code.

## What's tested

1. **POST broadcasts to viewers** - Messages posted to a room are broadcast to connected Socket.io clients
2. **Authorization** - Only the original creator (with the correct secret) can write to a room
3. **DELETE room** - Rooms can be deleted by the authorized user
4. **Existing content** - New viewers receive existing room content when they join

## Running locally

Start MongoDB:
```bash
docker run -d -p 27017:27017 mongo:5.0
```

Run tests:
```bash
MONGODB_URI=mongodb://localhost:27017/shellshare_test npm test
```

## CI

Tests run automatically on GitHub Actions using a MongoDB service container.
