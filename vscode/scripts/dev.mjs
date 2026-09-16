import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));
const port = Number(process.env.AGT_DEV_PORT ?? 5175);

const mime = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
};

function shell(title, script, css) {
  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>${title}</title>
<link rel="stylesheet" href="${css}">
</head>
<body>
<div id="agt-root"></div>
<script src="${script}"></script>
</body>
</html>`;
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url ?? '/', 'http://localhost');
  try {
    if (url.pathname === '/') {
      res.writeHead(302, { location: '/chat.html' });
      res.end();
      return;
    }
    if (url.pathname === '/settings.html') {
      res.writeHead(200, { 'content-type': mime['.html'] });
      res.end(shell('AGT Settings', '/dist/webview-settings.js', '/dist/webview-settings.css'));
      return;
    }
    if (url.pathname === '/chat.html') {
      res.writeHead(200, { 'content-type': mime['.html'] });
      res.end(shell('AGT Chat', '/dist/webview-chat.js', '/dist/webview-chat.css'));
      return;
    }
    const safe = normalize(url.pathname).replace(/^(\.\.[/\\])+/, '');
    const file = join(root, safe);
    if (!file.startsWith(root)) {
      throw new Error('path traversal');
    }
    const data = await readFile(file);
    res.writeHead(200, { 'content-type': mime[extname(file)] ?? 'application/octet-stream' });
    res.end(data);
  } catch {
    res.writeHead(404, { 'content-type': 'text/plain' });
    res.end('not found');
  }
});

server.listen(port, '127.0.0.1', () => {
  console.log(`AGT webview preview:`);
  console.log(`  http://127.0.0.1:${port}/chat.html`);
  console.log(`  http://127.0.0.1:${port}/settings.html`);
});
