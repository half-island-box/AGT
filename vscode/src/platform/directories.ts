import { readdirSync, type Dirent } from 'node:fs';
import { homedir } from 'node:os';
import { isAbsolute, relative, resolve } from 'node:path';
import * as vscode from 'vscode';
import type { AgtApi } from '../core/api';
import type { ConsoleDirectoryInfo, ConsoleDirectoryListing } from '../core/types';

/** Same cap as the daemon's ``fs.list`` handler. */
const MAX_DIRECTORY_ENTRIES = 1000;

function isPathUnder(child: string, root: string): boolean {
  const rel = relative(root, child);
  return rel === '' || (!rel.startsWith('..') && !isAbsolute(rel));
}

/**
 * Roots for browsing the local machine: the open workspace folders plus the
 * user's home directory. This keeps the picker bounded to where an agent can
 * reasonably run while still letting the user reach home from a workspace.
 */
function localRoots(): string[] {
  const roots: string[] = [];
  for (const folder of vscode.workspace.workspaceFolders ?? []) {
    if (folder.uri.fsPath) roots.push(resolve(folder.uri.fsPath));
  }
  const home = homedir();
  if (home && !roots.includes(home)) roots.push(home);
  return roots.length ? roots : [homedir()];
}

function listLocal(rawPath: string | null): ConsoleDirectoryListing {
  const roots = localRoots();
  let current: string;
  if (rawPath) {
    const text = rawPath.trim();
    if (!text) throw new Error('path cannot be blank');
    if (!isAbsolute(text)) throw new Error('path must be absolute');
    current = resolve(text);
    if (!roots.some((root) => isPathUnder(current, root))) {
      throw new Error('path is outside the local working directories');
    }
  } else {
    current = roots[0];
  }

  let entries: Dirent[];
  try {
    entries = readdirSync(current, { withFileTypes: true });
  } catch (err) {
    throw new Error(`cannot read directory: ${String(err)}`);
  }

  let parent: string | null = null;
  const parentCandidate = resolve(current, '..');
  if (parentCandidate !== current && roots.some((root) => isPathUnder(parentCandidate, root))) {
    parent = parentCandidate;
  }

  const directories: ConsoleDirectoryInfo[] = [];
  for (const entry of entries) {
    if (directories.length >= MAX_DIRECTORY_ENTRIES) break;
    if (!entry.isDirectory()) continue;
    const resolved = resolve(current, entry.name);
    if (!roots.some((root) => isPathUnder(resolved, root))) continue;
    directories.push({ name: entry.name, path: resolved });
  }
  directories.sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase()));

  return { roots, parent, current, directories };
}

/**
 * Answer ``projects.directories`` for the extension host. Remote daemons are
 * proxied to the Server's reverse-dial ``fs.list`` (their disk is unreachable
 * from here); the local machine is listed with ``node:fs``.
 */
export async function listDirectories(
  api: AgtApi,
  daemonId: string | null,
  path: string | null,
): Promise<ConsoleDirectoryListing> {
  if (daemonId) return api.daemonDirectories(daemonId, path);
  return listLocal(path);
}
