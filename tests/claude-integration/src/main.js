// Main application entry point
const express = require('express');
const config = require('./config');
const utils = require('./utils');

const app = express();

app.get('/api/config', (req, res) => {
  res.json(config.getAll());
});

app.get('/api/status', (req, res) => {
  res.json({
    status: 'ok',
    version: config.get('version'),
    uptime: process.uptime()
  });
});

app.get('/api/data/:id', (req, res) => {
  const id = parseInt(req.params.id);
  const data = utils.generateData(id);
  res.json(data);
});

if (require.main === module) {
  const port = config.get('port');
  app.listen(port, () => {
    console.log(`Server running on port ${port}`);
  });
}

module.exports = app;
