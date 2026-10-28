import { marked } from 'marked';
import DOMPurify from 'dompurify';

/**
 * Render a plain-text value as sanitized Markdown HTML.
 *
 * Used by the chat view for agent answers and by the SOP inspector for
 * execution logs so both surfaces share the same Markdown treatment.
 */
export function renderMarkdown(value: string): string {
  const html = marked.parse(value, {
    async: false,
    gfm: true,
    breaks: true,
  }) as string;
  return DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ['style', 'form'],
    FORBID_ATTR: ['style'],
  });
}
