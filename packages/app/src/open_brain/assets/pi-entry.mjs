/** Normal Pi SDK entry point. Private reads require a separately trusted host.
 * This subprocess adapter always has local_only=false and grants no confinement.
 * The host API in pi-memory.mjs remains available to sealed local Pi runtimes.
 */
import { spawn } from 'node:child_process';
import { isAbsolute } from 'node:path';
import { createPiMemoryExtension } from './pi-memory.mjs';

const disabled = () => process.env.OPEN_BRAIN_HARNESS_DISABLED === '1';
const searchSchema = { type: 'object', properties: {
  query: { type: 'string', minLength: 1, maxLength: 2048 },
  limit: { type: 'integer', minimum: 1, maximum: 5, default: 5 },
}, required: ['query'], additionalProperties: false };

function invoke(options, action, request) {
  return new Promise((resolve, reject) => {
    const { signal, ...payload } = request;
    if (disabled() || payload.local_only === true || signal?.aborted) {
      reject(new Error('MEMORY_REFUSED')); return;
    }
    const raw = JSON.stringify({ ...payload, local_only: false });
    if (Buffer.byteLength(raw) > 9 * 1024 * 1024) {
      reject(new Error('MEMORY_INPUT_LIMIT')); return;
    }
    const child = spawn(options.runtime, ['harness', action, '--config', options.config,
      '--client', 'pi'], { shell: false, detached: true, stdio: ['pipe', 'pipe', 'ignore'] });
    let output = '', settled = false;
    const stop = () => { try { process.kill(-child.pid, 'SIGKILL'); } catch { child.kill('SIGKILL'); } };
    const finish = (error, response) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
      if (error) { stop(); reject(error); } else resolve(response);
    };
    const abort = () => finish(new Error('MEMORY_ABORTED'));
    const timer = setTimeout(() => finish(new Error('MEMORY_TIMEOUT')), action === 'enqueue' ? 1900 : 2900);
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) abort();
    child.on('error', () => finish(new Error('MEMORY_UNAVAILABLE')));
    child.stdin.on('error', () => {});
    child.stdout.on('data', data => {
      output += data;
      if (Buffer.byteLength(output) > 256 * 1024) finish(new Error('MEMORY_RESPONSE_LIMIT'));
    });
    child.on('close', code => {
      try {
        const response = JSON.parse(output);
        // Retain metadata-only local-host refusal for the extension to handle.
        if (code !== 0 && response?.requires_local_only !== true) throw new Error();
        if (disabled()) throw new Error();
        finish(null, response);
      } catch { finish(new Error('MEMORY_UNAVAILABLE')); }
    });
    child.stdin.end(raw);
  });
}

function drain(options) {
  return new Promise((resolve, reject) => {
    if (disabled()) { resolve(); return; }
    const child = spawn(options.runtime, ['harness', 'drain', '--config', options.config,
      '--client', 'pi'], { shell: false, detached: true, stdio: 'ignore' });
    const timer = setTimeout(() => { child.kill('SIGKILL'); reject(new Error('MEMORY_TIMEOUT')); }, 100);
    child.once('error', () => { clearTimeout(timer); reject(new Error('MEMORY_UNAVAILABLE')); });
    child.once('spawn', () => { clearTimeout(timer); child.unref(); resolve(); });
  });
}

export default function configurePiMemory(pi, options) {
  if (!options || !isAbsolute(options.runtime ?? '') || !isAbsolute(options.config ?? '')
      || options.runtime.includes('\0') || options.config.includes('\0')) {
    throw new Error('MEMORY_HOST_CONTRACT_INVALID');
  }
  let context;
  const host = {
    on: (name, handler) => pi.on(name, async (event, ctx) => {
      if (disabled()) return;
      context = ctx;
      return handler(event, ctx);
    }),
    registerTool: tool => pi.registerTool({ ...tool, execute: async (...args) => {
      if (disabled()) return { content: [{ type: 'text', text: 'Open Brain memory disabled.' }],
        details: { status: 'disabled' }, isError: true };
      context = args[4];
      return tool.execute(...args);
    } }),
  };
  const bridge = {
    capture: async request => {
      const response = await invoke(options, 'enqueue', request);
      if (response?.status === 'queued') {
        await drain(options);
        context?.ui?.notify?.('Open Brain capture queued locally; delivery pending.', 'info');
      }
      return response;
    },
    recall: request => invoke(options, 'recall', request),
    search: request => invoke(options, 'search', request),
    source: request => invoke(options, 'source', request),
  };
  createPiMemoryExtension({ bridge, searchSchema, project: process.cwd(), provider: 'pi',
    sessionManager: {
      getSessionId: () => context?.sessionManager.getSessionId(),
      getBranch: () => context?.sessionManager.getBranch(),
      getHeader: () => context?.sessionManager.getHeader(),
    },
    localOnlyHost: {
      assertLocalOnly: () => false,
      requireLocalOnly: async () => { throw new Error('MEMORY_LOCAL_ONLY_UNENFORCED'); },
    },
  })(host);
}
