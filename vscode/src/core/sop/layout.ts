/**
 * Deterministic DAG layout for the SOP canvas.
 *
 * Layering is longest-path (see {@link layers}); nodes inside a layer keep
 * their YAML declaration order. Columns are vertically centered so a
 * single-node branch sits next to the middle of a wide layer.
 *
 * Manual positions are a *view* concern, never part of the SOP document
 * (the DSL carries no layout), so they live in the editor's workspace
 * state and simply override the computed point of the nodes they cover.
 */

import { layers } from './graph';
import type { SopDoc } from './model';

export interface SopPoint {
  x: number;
  y: number;
}

export const NODE_W = 208;
export const NODE_H = 88;
export const COL_GAP = 76;
export const ROW_GAP = 22;

export function computeLayout(
  doc: SopDoc,
  stored: Record<string, SopPoint> = {},
): Record<string, SopPoint> {
  const layerOf = layers(doc);
  const byLayer = new Map<number, string[]>();
  for (const node of doc.nodes) {
    const layer = layerOf.get(node.name) ?? 0;
    const bucket = byLayer.get(layer);
    if (bucket) {
      bucket.push(node.name);
    } else {
      byLayer.set(layer, [node.name]);
    }
  }

  const tallest = Math.max(
    1,
    ...[...byLayer.values()].map((names) => names.length),
  );
  const positions: Record<string, SopPoint> = {};
  for (const [layer, names] of byLayer) {
    const columnHeight = names.length * NODE_H + (names.length - 1) * ROW_GAP;
    const columnTop = ((tallest * NODE_H + (tallest - 1) * ROW_GAP) - columnHeight) / 2;
    names.forEach((name, row) => {
      positions[name] = {
        x: layer * (NODE_W + COL_GAP),
        y: columnTop + row * (NODE_H + ROW_GAP),
      };
    });
  }

  for (const [name, point] of Object.entries(stored)) {
    if (positions[name] && Number.isFinite(point.x) && Number.isFinite(point.y)) {
      positions[name] = { x: point.x, y: point.y };
    }
  }
  return positions;
}

/** Bounding box of a laid-out graph, used for "fit to view". */
export function layoutBounds(
  positions: Record<string, SopPoint>,
): { width: number; height: number } {
  let width = NODE_W;
  let height = NODE_H;
  for (const point of Object.values(positions)) {
    width = Math.max(width, point.x + NODE_W);
    height = Math.max(height, point.y + NODE_H);
  }
  return { width, height };
}
