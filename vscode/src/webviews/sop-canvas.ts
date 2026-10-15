/**
 * DAG canvas — DOM nodes + SVG edges, no framework.
 *
 * Two modes share this widget: ``editable`` (canvas drag, port-to-port
 * connect, double-click rename) and ``readonly`` (the future run monitor
 * renders live node status here). Keeping one renderer is deliberate: the
 * monitor must never disagree with the editor about the graph.
 */

import { NODE_H, NODE_W, layoutBounds, type SopPoint } from '../core/sop/layout';
import { providerLogo } from '../core/icons';

export interface CanvasNode {
  name: string;
  /** Effective provider label; rendered as a brand icon on the canvas. */
  provider: string;
  badge: string;
  selected: boolean;
  /** Drives the node outline color (info = neutral hint). */
  problem: 'error' | 'warning' | 'info' | null;
  /** Run status for the read-only monitor ('done', 'running', …). */
  status?: string;
}

export interface CanvasOptions {
  editable: boolean;
  nodes: CanvasNode[];
  edges: { from: string; to: string }[];
  positions: Record<string, SopPoint>;
  selected: string | null;
  /** Restored pan/zoom (the webview re-renders on every edit). */
  view?: { scale: number; offset: SopPoint };
  onSelect(name: string | null): void;
  /** Return false to reject the drop (e.g. the edge would close a cycle). */
  onConnect(from: string, to: string): boolean;
  onDeleteEdge(from: string, to: string): void;
  onMove(name: string, point: SopPoint): void;
  onRename(name: string): void;
  onAddNode(): void;
}

const MIN_ZOOM = 0.35;
const MAX_ZOOM = 2;

export class SopCanvas {
  private scale = 1;
  private offset: SopPoint = { x: 24, y: 18 };
  private drag:
    | { kind: 'node'; name: string; grabX: number; grabY: number }
    | { kind: 'pan'; startX: number; startY: number; originX: number; originY: number }
    | { kind: 'link'; from: string; x: number; y: number }
    | null = null;
  private positions: Record<string, SopPoint>;
  /**
   * Local mirror of the selection: the canvas re-renders itself at the end
   * of a drag, and must not lose the highlight the host just applied.
   */
  private selectedName: string | null;

  constructor(
    private readonly host: HTMLElement,
    private options: CanvasOptions,
  ) {
    this.positions = { ...options.positions };
    this.selectedName = options.selected;
    if (options.view) {
      this.scale = options.view.scale;
      this.offset = { ...options.view.offset };
    }
    this.host.addEventListener('pointerdown', this.onPointerDown);
    window.addEventListener('pointermove', this.onPointerMove);
    window.addEventListener('pointerup', this.onPointerUp);
    window.addEventListener('pointercancel', this.onPointerUp);
    this.host.addEventListener('dblclick', this.onDoubleClick);
    this.host.addEventListener('wheel', this.onWheel, { passive: false });
    this.host.addEventListener('contextmenu', (event) => event.preventDefault());
  }

  update(options: Partial<CanvasOptions>, positions?: Record<string, SopPoint>): void {
    this.options = { ...this.options, ...options };
    if (options.selected !== undefined) {
      this.selectedName = options.selected;
    }
    if (positions) {
      this.positions = { ...positions };
    }
    this.render();
  }

  getView(): { scale: number; offset: SopPoint } {
    return { scale: this.scale, offset: { ...this.offset } };
  }

  fit(): void {
    const bounds = layoutBounds(this.positions);
    const rect = this.host.getBoundingClientRect();
    const usable = { w: Math.max(120, rect.width - 48), h: Math.max(120, rect.height - 36) };
    const scale = Math.min(usable.w / bounds.width, usable.h / bounds.height, 1.4);
    this.scale = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, scale));
    this.offset = { x: 24, y: 18 };
    this.render();
  }

  zoom(delta: number): void {
    this.scale = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, this.scale + delta));
    this.render();
  }

  dispose(): void {
    this.host.removeEventListener('pointerdown', this.onPointerDown);
    window.removeEventListener('pointermove', this.onPointerMove);
    window.removeEventListener('pointerup', this.onPointerUp);
    window.removeEventListener('pointercancel', this.onPointerUp);
    this.host.removeEventListener('dblclick', this.onDoubleClick);
    this.host.removeEventListener('wheel', this.onWheel);
  }

  render(): void {
    const world = layoutBounds(this.positions);
    const width = world.width + 320;
    const height = world.height + 240;
    const edgePath = this.options.edges
      .filter((edge) => this.positions[edge.from] && this.positions[edge.to])
      .map((edge) => {
        const from = this.positions[edge.from];
        const to = this.positions[edge.to];
        const x1 = from.x + NODE_W;
        const y1 = from.y + NODE_H / 2;
        const x2 = to.x;
        const y2 = to.y + NODE_H / 2;
        const dx = Math.max(28, Math.abs(x2 - x1) * 0.5);
        return `<path class="agd-sop-edge" data-from="${escapeAttr(edge.from)}" data-to="${escapeAttr(
          edge.to,
        )}" d="M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}" marker-end="url(#agd-sop-arrow)"/>`;
      })
      .join('');

    const nodes = this.options.nodes
      .map((node) => {
        const point = this.positions[node.name] ?? { x: 0, y: 0 };
        const classes = [
          'agd-sop-node',
          node.name === this.selectedName ? 'is-selected' : '',
          node.problem === 'error' ? 'has-error' : '',
          node.problem === 'warning' ? 'has-warning' : '',
          node.problem === 'info' ? 'has-info' : '',
          node.status ? `is-${node.status}` : '',
          this.options.editable ? '' : 'is-readonly',
        ]
          .filter(Boolean)
          .join(' ');
        return `
        <div class="${classes}" data-node="${escapeAttr(node.name)}" data-status="${escapeAttr(node.status ?? '')}" style="left:${point.x}px;top:${point.y}px;width:${NODE_W}px;height:${NODE_H}px">
          ${
            this.options.editable
              ? `<span class="agd-sop-port agd-sop-port-in" data-port="in" data-node="${escapeAttr(node.name)}"></span>
                 <span class="agd-sop-port agd-sop-port-out" data-port="out" data-node="${escapeAttr(node.name)}"></span>`
              : ''
          }
          <div class="agd-sop-node-title">${escapeHtml(node.name)}</div>
          <div class="agd-sop-node-sub" title="${escapeAttr(node.provider)}">
            ${node.provider ? providerLogo(node.provider) : '<span class="agd-sop-node-provider-none">—</span>'}
          </div>
          ${node.badge ? `<div class="agd-sop-node-badge">${escapeHtml(node.badge)}</div>` : ''}
        </div>`;
      })
      .join('');

    const temp = this.drag?.kind === 'link' ? this.tempLinkPath() : '';

    this.host.innerHTML = `
      <div class="agd-sop-world" style="transform: translate(${this.offset.x}px, ${this.offset.y}px) scale(${this.scale}); width:${width}px; height:${height}px">
        <svg class="agd-sop-edges" width="${width}" height="${height}">
          <defs>
            <marker id="agd-sop-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" class="agd-sop-arrow-head"/>
            </marker>
          </defs>
          ${edgePath}
          ${temp}
        </svg>
        ${nodes}
      </div>
      <div class="agd-sop-canvas-hint">${escapeHtml(this.scaleLabel())}</div>`;
  }

  private scaleLabel(): string {
    return `${Math.round(this.scale * 100)}%`;
  }

  private tempLinkPath(): string {
    if (this.drag?.kind !== 'link') {
      return '';
    }
    const from = this.positions[this.drag.from] ?? { x: 0, y: 0 };
    const x1 = from.x + NODE_W;
    const y1 = from.y + NODE_H / 2;
    return `<line class="agd-sop-edge is-temp" x1="${x1}" y1="${y1}" x2="${this.drag.x}" y2="${this.drag.y}"/>`;
  }

  private updateTempLink(): void {
    if (this.drag?.kind !== 'link') {
      return;
    }
    const line = this.host.querySelector<SVGLineElement>('line.is-temp');
    if (line) {
      line.setAttribute('x2', String(this.drag.x));
      line.setAttribute('y2', String(this.drag.y));
    } else {
      const svg = this.host.querySelector('svg.agd-sop-edges');
      if (svg) {
        svg.insertAdjacentHTML('beforeend', this.tempLinkPath());
      }
    }
  }

  /** Highlight a node as a valid/invalid drop target while linking. */
  private markDropTarget(clientX: number, clientY: number, from: string): void {
    const element = document.elementFromPoint(clientX, clientY);
    const nodeEl = element?.closest('[data-node]') as HTMLElement | null;
    for (const other of this.host.querySelectorAll('.agd-sop-node.is-drop')) {
      other.classList.remove('is-drop');
    }
    if (nodeEl && nodeEl.dataset.node !== from) {
      nodeEl.classList.add('is-drop');
    }
  }

  private toWorld(clientX: number, clientY: number): SopPoint {
    const rect = this.host.getBoundingClientRect();
    return {
      x: (clientX - rect.left - this.offset.x) / this.scale,
      y: (clientY - rect.top - this.offset.y) / this.scale,
    };
  }

  private onPointerDown = (event: PointerEvent): void => {
    if (event.button !== 0) {
      return;
    }
    event.preventDefault();
    this.host.classList.add('is-dragging');
    const target = event.target as HTMLElement;
    const port = target.closest('[data-port]') as HTMLElement | null;
    const nodeEl = target.closest('[data-node]') as HTMLElement | null;
    if (port && port.dataset.port === 'out' && port.dataset.node) {
      if (!this.options.editable) {
        return;
      }
      const point = this.toWorld(event.clientX, event.clientY);
      this.drag = { kind: 'link', from: port.dataset.node, x: point.x, y: point.y };
      capturePointer(this.host, event.pointerId);
      return;
    }
    if (nodeEl && nodeEl.dataset.node) {
      const nodeName = nodeEl.dataset.node;
      const world = this.toWorld(event.clientX, event.clientY);
      const point = this.positions[nodeName] ?? { x: 0, y: 0 };
      this.drag = {
        kind: 'node',
        name: nodeName,
        grabX: world.x - point.x,
        grabY: world.y - point.y,
      };
      this.selectedName = nodeName;
      this.options.onSelect(nodeName);
      capturePointer(this.host, event.pointerId);
      return;
    }
    this.drag = {
      kind: 'pan',
      startX: event.clientX,
      startY: event.clientY,
      originX: this.offset.x,
      originY: this.offset.y,
    };
    this.selectedName = null;
    this.options.onSelect(null);
  };

  private onPointerMove = (event: PointerEvent): void => {
    if (!this.drag) {
      return;
    }
    if (this.drag.kind === 'pan') {
      this.offset = {
        x: this.drag.originX + (event.clientX - this.drag.startX),
        y: this.drag.originY + (event.clientY - this.drag.startY),
      };
      this.applyTransform();
      return;
    }
    if (this.drag.kind === 'node') {
      const world = this.toWorld(event.clientX, event.clientY);
      const point = {
        x: Math.round(world.x - this.drag.grabX),
        y: Math.round(world.y - this.drag.grabY),
      };
      this.positions[this.drag.name] = point;
      this.applyNodePosition(this.drag.name, point);
      return;
    }
    const world = this.toWorld(event.clientX, event.clientY);
    this.drag = { ...this.drag, x: world.x, y: world.y };
    this.updateTempLink();
    this.markDropTarget(event.clientX, event.clientY, this.drag.from);
  };

  private onPointerUp = (event: PointerEvent): void => {
    const drag = this.drag;
    this.drag = null;
    this.host.classList.remove('is-dragging');
    if (!drag) {
      return;
    }
    if (drag.kind === 'node') {
      this.options.onMove(drag.name, this.positions[drag.name]);
      this.render();
      return;
    }
    if (drag.kind === 'link') {
      const element = document.elementFromPoint(event.clientX, event.clientY);
      const nodeEl = element?.closest('[data-node]') as HTMLElement | null;
      const to = nodeEl?.dataset.node;
      if (to && to !== drag.from) {
        const accepted = this.options.onConnect(drag.from, to);
        if (!accepted) {
          this.host.classList.add('is-rejected');
          window.setTimeout(() => this.host.classList.remove('is-rejected'), 320);
        }
      }
      this.render();
    }
  };

  private onDoubleClick = (event: MouseEvent): void => {
    const target = event.target as HTMLElement;
    const nodeEl = target.closest('[data-node]') as HTMLElement | null;
    if (nodeEl?.dataset.node && this.options.editable) {
      this.options.onRename(nodeEl.dataset.node);
      return;
    }
    if (!nodeEl && this.options.editable) {
      this.options.onAddNode();
    }
  };

  private onWheel = (event: WheelEvent): void => {
    if (!event.ctrlKey && !event.metaKey) {
      this.offset = {
        x: this.offset.x - event.deltaX,
        y: this.offset.y - event.deltaY,
      };
      this.applyTransform();
      event.preventDefault();
      return;
    }
    this.zoom(event.deltaY > 0 ? -0.08 : 0.08);
    event.preventDefault();
  };

  private applyTransform(): void {
    const world = this.host.querySelector<HTMLElement>('.agd-sop-world');
    if (world) {
      world.style.transform = `translate(${this.offset.x}px, ${this.offset.y}px) scale(${this.scale})`;
    }
  }

  private applyNodePosition(name: string, point: SopPoint): void {
    const element = this.host.querySelector<HTMLElement>(`[data-node="${cssEscape(name)}"]`);
    if (element) {
      element.style.left = `${point.x}px`;
      element.style.top = `${point.y}px`;
    }
  }
}

function escapeHtml(value: string): string {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

function escapeAttr(value: string): string {
  return escapeHtml(value);
}

function cssEscape(value: string): string {
  return value.replace(/["\\]/g, '\\$&');
}

/**
 * Pointer capture is a nice-to-have (it keeps a drag alive outside the
 * element); synthetic pointers — browser preview, some hosts — reject it,
 * and losing capture must never break the interaction.
 */
function capturePointer(element: HTMLElement, pointerId: number): void {
  try {
    element.setPointerCapture(pointerId);
  } catch {
    // Pointer is not capturable here; pointermove/up on the host still fire.
  }
}
