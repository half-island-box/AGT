/**
 * Text-level helpers the editor host needs: SOP sniffing and the minimal
 * edit span for writing a mutated document back into the buffer.
 *
 * Kept in `core` (no `vscode` import) so both are covered by the plain
 * node test suite.
 */

/**
 * Cheap sniff for template discovery: a SOP document has a top-level
 * ``name`` and a block ``tasks`` key. Deliberately conservative — a
 * non-SOP YAML must never show up in the template list.
 */
export function looksLikeSop(text: string): boolean {
  const head = text.slice(0, 4000);
  return /^name:\s*\S/m.test(head) && /^tasks:\s*$/m.test(head);
}

/**
 * Character span that differs between the current and the next text
 * (common prefix and suffix trimmed), so a canvas edit lands as a small
 * buffer edit rather than a whole-file replacement.
 */
export function minimalSpan(
  current: string,
  next: string,
): { start: number; end: number; text: string } {
  let start = 0;
  const limit = Math.min(current.length, next.length);
  while (start < limit && current[start] === next[start]) {
    start += 1;
  }
  let endCurrent = current.length;
  let endNext = next.length;
  while (
    endCurrent > start &&
    endNext > start &&
    current[endCurrent - 1] === next[endNext - 1]
  ) {
    endCurrent -= 1;
    endNext -= 1;
  }
  return { start, end: endCurrent, text: next.slice(start, endNext) };
}
