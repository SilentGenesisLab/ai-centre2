"use client";

import {
  Children,
  isValidElement,
  type ReactNode,
  type SelectHTMLAttributes,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";

type SelectOption = { value: string; label: string; disabled: boolean };

function readOptions(children: ReactNode): SelectOption[] {
  const options: SelectOption[] = [];
  Children.forEach(children, child => {
    if (!isValidElement(child)) return;
    if (child.type === "option") {
      const props = child.props as { value?: string | number; children?: ReactNode; disabled?: boolean };
      const label = Children.toArray(props.children).join("");
      options.push({ value: String(props.value ?? label), label, disabled: Boolean(props.disabled) });
      return;
    }
    const props = child.props as { children?: ReactNode };
    if (props.children) options.push(...readOptions(props.children));
  });
  return options;
}

export function AppSelect({ children, className, value, defaultValue, disabled, onChange, "aria-label": ariaLabel, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  const options = readOptions(children);
  const [selected, setSelected] = useState(String(value ?? defaultValue ?? options[0]?.value ?? ""));
  const [open, setOpen] = useState(false);
  const menuId = useId();
  const rootRef = useRef<HTMLSpanElement>(null);
  const nativeRef = useRef<HTMLSelectElement>(null);
  const currentValue = value !== undefined ? String(value) : selected;

  useEffect(() => {
    const close = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, []);

  const choose = (next: string) => {
    if (disabled) return;
    if (value === undefined) setSelected(next);
    setOpen(false);
    if (nativeRef.current) {
      nativeRef.current.value = next;
      nativeRef.current.dispatchEvent(new Event("change", { bubbles: true }));
    }
  };
  const move = (direction: 1 | -1) => {
    const enabled = options.filter(option => !option.disabled);
    const current = enabled.findIndex(option => option.value === currentValue);
    choose(enabled[(current + direction + enabled.length) % enabled.length]?.value ?? currentValue);
  };
  const active = options.find(option => option.value === currentValue) ?? options[0];

  return <span className={`app-select ${open ? "is-open" : ""} ${disabled ? "is-disabled" : ""} ${className || ""}`} ref={rootRef}>
    <select {...props} ref={nativeRef} value={currentValue} disabled={disabled} onChange={onChange} className="native-select-proxy" tabIndex={-1} aria-hidden="true">{children}</select>
    <button type="button" className="app-select-trigger" role="combobox" aria-label={ariaLabel} aria-controls={menuId} aria-expanded={open} aria-haspopup="listbox" disabled={disabled} onClick={() => setOpen(current => !current)} onKeyDown={event => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); move(event.key === "ArrowDown" ? 1 : -1); }
      if (event.key === "Escape") setOpen(false);
    }}><span>{active?.label || "请选择"}</span><svg viewBox="0 0 16 16" aria-hidden><path d="m4 6 4 4 4-4" /></svg></button>
    {open && <span className="app-select-menu" id={menuId} role="listbox" aria-label={ariaLabel}>{options.map(option => <button type="button" role="option" aria-selected={option.value === currentValue} disabled={option.disabled} className={option.value === currentValue ? "is-selected" : ""} key={option.value} onClick={() => choose(option.value)}><span>{option.label}</span>{option.value === currentValue && <svg viewBox="0 0 16 16" aria-hidden><path d="m3 8 3 3 7-7" /></svg>}</button>)}</span>}
  </span>;
}
