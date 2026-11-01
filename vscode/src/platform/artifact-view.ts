import * as vscode from 'vscode';
import { createHash } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { dirname } from 'node:path';
import type { ServerLifecycle } from '../core/lifecycle';

const ARTIFACT_SCHEME = 'agt-artifact://';

export interface OpenArtifactParams {
  artifactUri: string;
  /** The producing/selected node's directory, when the UI knows it. */
  dir?: string | null;
  workspaceFolder?: string | null;
}

function artifactId(uri: string): string {
  const id = uri.startsWith(ARTIFACT_SCHEME)
    ? uri.slice(ARTIFACT_SCHEME.length)
    : uri;
  const trimmed = id.trim().replace(/^\/+/, '');
  if (!trimmed || trimmed.includes('/')) {
    throw new Error('invalid artifact URI');
  }
  return trimmed;
}

function relativeSegments(name: string): string[] {
  return name.replaceAll('\\', '/').split('/').filter((part) => {
    return part.length > 0 && part !== '.' && part !== '..';
  });
}

async function fileExists(uri: vscode.Uri): Promise<boolean> {
  try {
    await vscode.workspace.fs.stat(uri);
    return true;
  } catch {
    return false;
  }
}

async function sha256File(uri: vscode.Uri): Promise<string> {
  return new Promise((resolve, reject) => {
    const hash = createHash('sha256');
    const stream = createReadStream(uri.fsPath);
    stream.on('data', (chunk) => hash.update(chunk));
    stream.on('error', reject);
    stream.on('end', () => resolve(hash.digest('hex')));
  });
}

function uniqueRoots(...roots: Array<string | null | undefined>): string[] {
  return [...new Set(roots.filter((root): root is string => {
    return typeof root === 'string' && root.trim().length > 0;
  }).map((root) => root.trim()))];
}

export async function openArtifact(
  lifecycle: ServerLifecycle,
  params: OpenArtifactParams,
): Promise<{ source: 'local' | 'downloaded'; target: string }> {
  const id = artifactId(params.artifactUri);
  const artifact = await lifecycle.api.artifact(id);
  const segments = relativeSegments(artifact.name);
  if (segments.length === 0) {
    throw new Error(`artifact has an invalid file name: ${artifact.name}`);
  }

  const roots = uniqueRoots(params.dir, params.workspaceFolder);
  const candidates: vscode.Uri[] = [];
  // `agt artifact get <id>` writes the declared name's basename into the
  // current directory; keep plugin downloads in that same location.
  const basename = segments[segments.length - 1];
  for (const root of roots) {
    const rootUri = vscode.Uri.file(root);
    candidates.push(vscode.Uri.joinPath(rootUri, basename));
    candidates.push(vscode.Uri.joinPath(rootUri, ...segments));
    candidates.push(vscode.Uri.joinPath(rootUri, '.agt', 'outputs', ...segments));
    candidates.push(vscode.Uri.joinPath(rootUri, '.agt', 'artifacts', id, basename));
  }
  for (const candidate of candidates) {
    if (await fileExists(candidate) && await sha256File(candidate) === artifact.sha256) {
      await vscode.commands.executeCommand('vscode.open', candidate);
      return { source: 'local', target: candidate.fsPath };
    }
  }

  const workspaceRoot = params.workspaceFolder?.trim();
  if (!workspaceRoot) {
    throw new Error('open a workspace folder to download this artifact');
  }

  return vscode.window.withProgress({
    location: vscode.ProgressLocation.Notification,
    title: `Pulling artifact ${artifact.name}...`,
    cancellable: false,
  }, async () => {
    const bytes = await lifecycle.api.artifactContent(id);
    const target = vscode.Uri.joinPath(vscode.Uri.file(workspaceRoot), basename);
    await vscode.workspace.fs.createDirectory(vscode.Uri.file(dirname(target.fsPath)));
    await vscode.workspace.fs.writeFile(target, bytes);
    await vscode.commands.executeCommand('vscode.open', target);
    return { source: 'downloaded' as const, target: target.fsPath };
  });
}
