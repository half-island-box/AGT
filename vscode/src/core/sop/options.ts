/**
 * Select-option building for the inspector.
 *
 * The invariant this exists for: **a select must always display the value
 * the document actually holds.** A naive list of "known values + none"
 * breaks that as soon as the document references something not in the list
 * (an identity that was deleted, a daemon that is not registered right
 * now): the browser then falls back to the first option, the UI shows
 * 「不指定」, and re-picking it fires no `change` event — the value looks
 * editable but is not.
 */

export interface SopSelectOption {
  value: string;
  label: string;
  selected: boolean;
  /** True for the synthetic "current value, not in the list" entry. */
  unmatched?: boolean;
}

export interface BuildOptionsInput {
  /** Known values (order preserved; duplicates removed). */
  values: string[];
  /** The document's current value (``''`` = none). */
  current: string;
  /** Label for the empty choice. */
  noneLabel: string;
  /** Label for a current value that is absent from ``values``. */
  unmatchedLabel: (value: string) => string;
}

export function buildOptions({
  values,
  current,
  noneLabel,
  unmatchedLabel,
}: BuildOptionsInput): SopSelectOption[] {
  const unique = [...new Set(values.filter((value) => value !== ''))];
  const options: SopSelectOption[] = [
    { value: '', label: noneLabel, selected: current === '' },
    ...unique.map((value) => ({
      value,
      label: value,
      selected: value === current,
    })),
  ];
  if (current !== '' && !unique.includes(current)) {
    options.splice(1, 0, {
      value: current,
      label: unmatchedLabel(current),
      selected: true,
      unmatched: true,
    });
  }
  return options;
}

/** Render options as ``<option>`` markup (labels are escaped by the caller). */
export function renderOptions(
  options: SopSelectOption[],
  escape: (value: string) => string,
): string {
  return options
    .map(
      (option) =>
        `<option value="${escape(option.value)}"${option.selected ? ' selected' : ''}${
          option.unmatched ? ' data-unmatched="1"' : ''
        }>${escape(option.label)}</option>`,
    )
    .join('');
}
