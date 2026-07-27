// Utility functions
const crypto = require('crypto');

function generateData(id) {
  return {
    id,
    hash: crypto.createHash('sha256').update(String(id)).digest('hex'),
    timestamp: Date.now(),
    value: Math.random() * 100
  };
}

function formatResponse(status, data, message) {
  return { status, data, message, timestamp: new Date().toISOString() };
}

function validateId(id) {
  const num = parseInt(id);
  if (isNaN(num) || num < 0) throw new Error('Invalid ID: must be a positive number');
  return num;
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

module.exports = { generateData, formatResponse, validateId, sleep };
