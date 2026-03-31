/**
 * workers/healthcheck.js
 * Health check for Docker HEALTHCHECK directive.
 * Exits 0 if worker is healthy, 1 if not.
 */

const http = require("http");

const options = {
  host:    "localhost",
  port:    3000,
  path:    "/health",
  timeout: 3000,
};

const req = http.request(options, (res) => {
  let data = "";
  res.on("data", chunk => { data += chunk; });
  res.on("end", () => {
    if (res.statusCode === 200) {
      const body = JSON.parse(data);
      console.log(`Health: ${body.status} | Workers: ${body.workers} | Queues: ${body.queues}`);
      process.exit(0);
    } else {
      console.error(`Unhealthy status code: ${res.statusCode}`);
      process.exit(1);
    }
  });
});

req.on("error", (err) => {
  console.error(`Health check failed: ${err.message}`);
  process.exit(1);
});

req.on("timeout", () => {
  console.error("Health check timed out");
  req.destroy();
  process.exit(1);
});

req.end();
