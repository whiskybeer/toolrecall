// Configuration module
const fs = require('fs');
const path = require('path');

const defaults = {
  port: 3000,
  version: '1.0.0',
  logLevel: 'info',
  maxRetries: 3,
  cacheTimeout: 5000,
  database: {
    host: 'localhost',
    port: 5432,
    name: 'toolrecall_test'
  }
};

let config = { ...defaults };

function load(filePath) {
  if (fs.existsSync(filePath)) {
    const raw = fs.readFileSync(filePath, 'utf-8');
    const overrides = JSON.parse(raw);
    config = { ...defaults, ...overrides };
  }
  return config;
}

function get(key) {
  return key.split('.').reduce((obj, k) => obj?.[k], config);
}

function getAll() {
  return { ...config };
}

module.exports = { load, get, getAll };
