const { onRequest } = require("firebase-functions/v2/https");
const logger = require("firebase-functions/logger");
const admin = require("firebase-admin");
const { createStore } = require("./store");
const { createFirestoreAdapter } = require("./stores/firestore");
const { createRouter } = require("./router");

admin.initializeApp();
admin.firestore().settings({ ignoreUndefinedProperties: true });

// Firebase keeps the existing API and rollout default. The self-hosted entry
// point uses the same handlers/refresh coordinator with a different adapter.
exports.broker = onRequest(
  { cors: false, timeoutSeconds: 120, secrets: ["BROKER_BOOTSTRAP_TOKEN"] },
  createRouter(createStore(createFirestoreAdapter(admin.firestore())), { logger })
);
