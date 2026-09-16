const esbuild = require('esbuild');

const production = process.argv.includes('--production');
const watch = process.argv.includes('--watch');

/** Extension host entry — Node context, `vscode` is provided by the host. */
const extension = {
  entryPoints: ['src/platform/extension.ts'],
  bundle: true,
  platform: 'node',
  format: 'cjs',
  target: 'node18',
  external: ['vscode'],
  outfile: 'dist/extension.js',
  sourcemap: !production,
  minify: production,
  logLevel: 'info',
};

/** Settings webview entry — browser context, also previewable standalone. */
const webviewSettings = {
  entryPoints: ['src/webviews/settings.ts'],
  bundle: true,
  platform: 'browser',
  format: 'iife',
  target: 'es2022',
  outfile: 'dist/webview-settings.js',
  sourcemap: false,
  minify: production,
  logLevel: 'info',
};

/** Chat webview entry — browser context, also previewable standalone. */
const webviewChat = {
  entryPoints: ['src/webviews/chat.ts'],
  bundle: true,
  platform: 'browser',
  format: 'iife',
  target: 'es2022',
  outfile: 'dist/webview-chat.js',
  sourcemap: false,
  minify: production,
  logLevel: 'info',
};

async function main() {
  if (watch) {
    const ctxExt = await esbuild.context(extension);
    const ctxWeb = await esbuild.context(webviewSettings);
    const ctxChat = await esbuild.context(webviewChat);
    await Promise.all([ctxExt.watch(), ctxWeb.watch(), ctxChat.watch()]);
  } else {
    await esbuild.build(extension);
    await esbuild.build(webviewSettings);
    await esbuild.build(webviewChat);
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
