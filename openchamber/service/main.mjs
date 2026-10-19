// Loopback proxy from the OpenChamber panel to Tarmac's local API. The panel
// runs in a sandboxed iframe and cannot reach 127.0.0.1 itself; OpenChamber
// starts this process and forwards the panel's serviceRequest calls here.
import http from 'node:http';
import {timingSafeEqual} from 'node:crypto';

const port = Number(process.env.OPENCHAMBER_SERVICE_PORT);
const token = process.env.OPENCHAMBER_SERVICE_TOKEN;
if (!port || !token) throw new Error('OpenChamber must launch the Tarmac service.');

const TARMAC = 'http://127.0.0.1:8767';
// OpenChamber gives a service request 20s; stay under it.
const TIMEOUT_MS = 18000;
const REFRESH_WAIT_SECONDS = 15;

function authorized(header) {
  const actual = Buffer.from(header || ''), expected = Buffer.from('Bearer ' + token);
  return actual.length === expected.length && timingSafeEqual(actual, expected);
}

async function tarmac(method, path) {
  try {
    const response = await fetch(TARMAC + path, {method, signal: AbortSignal.timeout(TIMEOUT_MS)});
    return {status: response.status, body: await response.text()};
  } catch (error) {
    const refused = error?.cause?.code === 'ECONNREFUSED';
    return {status: 503, body: JSON.stringify({error: refused
      ? 'Tarmac is not running on 127.0.0.1:8767. Start it with `tarmac serve` or `tarmac service install`.'
      : 'Tarmac did not answer: ' + (error?.message || 'unknown error')})};
  }
}

const server = http.createServer(async (req, res) => {
  const respond = ({status, body}) => {
    res.writeHead(status, {'content-type': 'application/json'});
    res.end(body);
  };
  if (!authorized(req.headers.authorization)) return respond({status: 401, body: '{"error":"Unauthorized"}'});
  const url = new URL(req.url, 'http://localhost');
  const slug = encodeURIComponent(url.searchParams.get('slug') || '');
  if (url.pathname === '/health') return respond({status: 200, body: '{"ready":true}'});
  // passive=1 reads the snapshot, which neither counts as a reader nor polls a provider.
  if (req.method === 'GET' && url.pathname === '/status')
    return respond(await tarmac('GET', url.searchParams.get('passive') ? '/api/v1/snapshot' : '/api/v1/status'));
  if (req.method === 'POST' && url.pathname === '/refresh' && slug)
    return respond(await tarmac('POST', `/api/v1/sources/${slug}/refresh?wait=${REFRESH_WAIT_SECONDS}`));
  if (req.method === 'POST' && url.pathname === '/login' && slug)
    return respond(await tarmac('POST', `/api/v1/sources/${slug}/login`));
  respond({status: 404, body: '{"error":"Unknown Tarmac route"}'});
});
server.listen(port, '127.0.0.1');
process.on('SIGTERM', () => server.close(() => process.exit(0)));
