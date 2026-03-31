/**
 * CortexBot Worker — Node.js BullMQ Job Processor
 * Phase 2 Complete: 14 queues + 2 daily/weekly crons
 *
 * Queues:
 *   dispatch_workflows  — Start new load dispatch workflow
 *   email_parse         — Process inbound email
 *   doc_ocr             — Extract fields from RC/BOL PDF
 *   transit_monitor     — GPS position check (every 15 min)
 *   hos_check           — HOS compliance check (every 15 min)
 *   weather_check       — Weather route scan (every 30 min)
 *   payment_followup    — Advance payment follow-up steps
 *   compliance_sweep    — Daily compliance + claims deadline check
 *   carrier_performance — Weekly carrier KPI scoring
 *   broker_scoring      — Weekly broker relationship scoring
 *   backhaul_search     — Search for next load from delivery city
 *   fraud_check         — Pre-booking fraud assessment
 *   driver_advance      — Issue EFS/Comdata advance code
 *   invoice_submit      — Submit invoice to factoring/broker
 */

require('dotenv').config({ path: '../.env.local' });

const { Queue, Worker, QueueScheduler, QueueEvents } = require('bullmq');
const axios = require('axios');

const REDIS_URL    = process.env.REDIS_URL || 'redis://redis:6379/0';
const API_BASE_URL = process.env.API_URL   || 'http://cortexbot-api:8000';

// Parse Redis connection from URL
function parseRedisUrl(url) {
  const u = new URL(url);
  return {
    host: u.hostname,
    port: parseInt(u.port || '6379'),
    db:   parseInt(u.pathname.replace('/', '') || '0'),
  };
}

const connection = parseRedisUrl(REDIS_URL);

// ── Call Python API ────────────────────────────────────────────
async function callApi(endpoint, data = {}) {
  try {
    const response = await axios.post(`${API_BASE_URL}${endpoint}`, data, {
      timeout: 120_000, // 2-minute timeout for long-running skills
    });
    return response.data;
  } catch (err) {
    const status  = err.response?.status;
    const message = err.response?.data?.error || err.message;
    throw new Error(`API call to ${endpoint} failed [${status}]: ${message}`);
  }
}

// ── Job Processor ──────────────────────────────────────────────
async function processJob(job) {
  const { name, data } = job;
  console.log(`[${new Date().toISOString()}] Processing job: ${name} | id=${job.id}`);

  switch (name) {
    // ── Phase 1 Jobs ────────────────────────────────────────────
    case 'start_dispatch_workflow':
      return callApi('/debug/start-workflow', data);

    case 'parse_email':
      return callApi(`/internal/process-email/${data.email_id}`);

    case 'process_ocr':
      return callApi('/internal/process-ocr', data);

    // ── Phase 2 Transit Jobs ─────────────────────────────────────
    case 'transit_gps_check':
      return callApi('/internal/transit-monitor', { load_id: data.load_id });

    case 'hos_compliance_check':
      return callApi('/internal/hos-check', {
        driver_id: data.driver_id,
        load_id:   data.load_id,
      });

    case 'weather_route_check':
      return callApi('/internal/weather-check', { load_id: data.load_id });

    // ── Phase 2 Financial Jobs ───────────────────────────────────
    case 'payment_followup_step':
      return callApi('/internal/payment-followup', {
        invoice_id: data.invoice_id,
        step:       data.step,
      });

    case 'submit_invoice':
      return callApi('/internal/process-ocr', {
        load_id: data.load_id,
        s3_url:  data.invoice_pdf_url,
      });

    case 'issue_driver_advance':
      return callApi('/api/advances/request', data);

    // ── Phase 2 Compliance & Scoring ────────────────────────────
    case 'daily_compliance_sweep':
      console.log('[CRON] Running daily compliance sweep...');
      await callApi('/internal/compliance-sweep');
      return callApi('/internal/claims-deadline-check');

    case 'weekly_carrier_performance':
      console.log('[CRON] Running weekly carrier performance scoring...');
      return callApi('/internal/carrier-performance', {});

    case 'weekly_broker_scoring':
      console.log('[CRON] Running weekly broker relationship scoring...');
      return callApi('/internal/broker-scoring', {});

    // ── Phase 2 Load Optimization Jobs ──────────────────────────
    case 'backhaul_search':
      return callApi('/internal/backhaul-search', data);

    case 'fraud_check':
      return callApi('/internal/fraud-check', {
        broker_mc:  data.broker_mc,
        load_id:    data.load_id,
        carrier_mc: data.carrier_mc,
      });

    default:
      console.warn(`Unknown job type: ${name}`);
      return { skipped: true, reason: `No handler for job type: ${name}` };
  }
}

// ── Queue Definitions ──────────────────────────────────────────
const QUEUES = [
  // Phase 1
  { name: 'cortex:dispatch_workflows', concurrency: 50 },
  { name: 'cortex:email_parse',        concurrency: 20 },
  { name: 'cortex:doc_ocr',            concurrency: 10 },
  // Phase 2 Transit (high frequency, high concurrency)
  { name: 'cortex:transit_monitor',    concurrency: 100 },
  { name: 'cortex:hos_check',          concurrency: 100 },
  { name: 'cortex:weather_check',      concurrency: 50  },
  // Phase 2 Financial
  { name: 'cortex:payment_followup',   concurrency: 20  },
  { name: 'cortex:invoice_submit',     concurrency: 20  },
  { name: 'cortex:driver_advance',     concurrency: 30  },
  // Phase 2 Scoring & Compliance (lower concurrency — batch-ish)
  { name: 'cortex:compliance_sweep',   concurrency: 5   },
  { name: 'cortex:carrier_performance', concurrency: 5  },
  { name: 'cortex:broker_scoring',     concurrency: 5   },
  // Phase 2 Optimization
  { name: 'cortex:backhaul_search',    concurrency: 30  },
  { name: 'cortex:fraud_check',        concurrency: 30  },
];

// ── Worker Retry Policy ────────────────────────────────────────
const DEFAULT_JOB_OPTIONS = {
  attempts:      3,
  backoff:       { type: 'exponential', delay: 2000 },
  removeOnComplete: { count: 500, age: 86400 },
  removeOnFail:     { count: 100, age: 604800 },
};

// ── Start All Workers ──────────────────────────────────────────
const workers = [];

for (const { name, concurrency } of QUEUES) {
  const worker = new Worker(
    name,
    processJob,
    {
      connection,
      concurrency,
      defaultJobOptions: DEFAULT_JOB_OPTIONS,
    }
  );

  worker.on('completed', (job, result) => {
    console.log(`✅ [${name}] Job ${job.id} completed`);
  });

  worker.on('failed', (job, err) => {
    console.error(`❌ [${name}] Job ${job?.id} failed: ${err.message}`);
  });

  worker.on('error', (err) => {
    console.error(`💥 [${name}] Worker error: ${err.message}`);
  });

  workers.push(worker);
  console.log(`🚀 Worker started: ${name} (concurrency=${concurrency})`);
}

// ── Queue instances for scheduling ────────────────────────────
const complianceQueue      = new Queue('cortex:compliance_sweep',   { connection });
const performanceQueue     = new Queue('cortex:carrier_performance', { connection });
const brokerScoringQueue   = new Queue('cortex:broker_scoring',     { connection });

// ── Daily Cron Jobs ────────────────────────────────────────────
// These use BullMQ's repeat feature to schedule recurring jobs

async function setupCronJobs() {
  // Daily compliance sweep — 06:00 UTC every day
  await complianceQueue.add(
    'daily_compliance_sweep',
    {},
    {
      repeat: { cron: '0 6 * * *' },
      jobId:  'daily-compliance-sweep',
    }
  );
  console.log('📅 Cron: daily_compliance_sweep at 06:00 UTC');

  // Weekly carrier performance — Monday 07:00 UTC
  await performanceQueue.add(
    'weekly_carrier_performance',
    {},
    {
      repeat: { cron: '0 7 * * 1' },
      jobId:  'weekly-carrier-performance',
    }
  );
  console.log('📅 Cron: weekly_carrier_performance every Monday 07:00 UTC');

  // Weekly broker scoring — Monday 07:30 UTC
  await brokerScoringQueue.add(
    'weekly_broker_scoring',
    {},
    {
      repeat: { cron: '30 7 * * 1' },
      jobId:  'weekly-broker-scoring',
    }
  );
  console.log('📅 Cron: weekly_broker_scoring every Monday 07:30 UTC');
}

setupCronJobs().catch(console.error);

// ── Helper: Enqueue Transit Monitoring for a Load ──────────────
// Called externally via Redis when a load reaches DISPATCHED status
const transitMonitorQueue = new Queue('cortex:transit_monitor', { connection });
const hosCheckQueue       = new Queue('cortex:hos_check',       { connection });
const weatherCheckQueue   = new Queue('cortex:weather_check',   { connection });
const backHaulQueue       = new Queue('cortex:backhaul_search', { connection });

async function enqueueTransitMonitoring(loadId, driverPhone, carrierId) {
  // GPS check every 15 minutes
  await transitMonitorQueue.add(
    'transit_gps_check',
    { load_id: loadId },
    {
      repeat: {
        cron:  '*/15 * * * *',
        jobId: `transit-${loadId}`,
      },
      jobId: `transit-${loadId}`,
    }
  );

  // Weather check every 30 minutes
  await weatherCheckQueue.add(
    'weather_route_check',
    { load_id: loadId },
    {
      repeat: {
        cron:  '*/30 * * * *',
        jobId: `weather-${loadId}`,
      },
    }
  );

  console.log(`📡 Transit monitoring scheduled for load ${loadId}`);
}

// Listen for Redis pub/sub message to start transit monitoring
// Published by orchestrator_phase2.py when load reaches DISPATCHED
const subscriber = require('ioredis');
const redisSub = new subscriber(connection);

redisSub.subscribe('cortex:transit:start', (err) => {
  if (err) console.error('Redis subscribe error:', err);
  else console.log('📡 Subscribed to cortex:transit:start channel');
});

redisSub.on('message', async (channel, message) => {
  if (channel === 'cortex:transit:start') {
    try {
      const { load_id, driver_id, carrier_id } = JSON.parse(message);
      await enqueueTransitMonitoring(load_id, driver_id, carrier_id);

      // Also add HOS check
      if (driver_id) {
        await hosCheckQueue.add(
          'hos_compliance_check',
          { driver_id, load_id },
          {
            repeat: {
              cron:  '*/15 * * * *',
              jobId: `hos-${load_id}`,
            },
          }
        );
      }

      // Schedule backhaul search (immediate, one-time)
      await backHaulQueue.add('backhaul_search', { load_id, carrier_id });

    } catch (e) {
      console.error('Transit start handler error:', e.message);
    }
  }
});

// ── Health Check HTTP Server ───────────────────────────────────
const http = require('http');
const server = http.createServer(async (req, res) => {
  if (req.url === '/health' && req.method === 'GET') {
    const healthStats = await Promise.all(
      workers.map(async (w) => ({
        name:     w.name,
        running:  w.isRunning(),
      }))
    );
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      status:        'ok',
      workers:       healthStats,
      total_workers: workers.length,
      timestamp:     new Date().toISOString(),
    }));
  } else {
    res.writeHead(404);
    res.end('Not found');
  }
});

server.listen(3000, () => {
  console.log('🌐 Worker health server listening on :3000');
});

// ── Graceful Shutdown ─────────────────────────────────────────
async function shutdown(signal) {
  console.log(`\n${signal} received — shutting down workers gracefully...`);
  await Promise.all(workers.map((w) => w.close()));
  server.close();
  redisSub.disconnect();
  console.log('✅ Workers shut down');
  process.exit(0);
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT',  () => shutdown('SIGINT'));

process.on('uncaughtException', (err) => {
  console.error('Uncaught exception:', err);
});

process.on('unhandledRejection', (reason) => {
  console.error('Unhandled rejection:', reason);
});
