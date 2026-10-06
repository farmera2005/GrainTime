import { cloneElement, isValidElement, ReactElement, ReactNode, useId } from "react";

/** Labelled form control; hint and error are linked to the input for screen readers. */
export function Field(props: { label: string; hint?: ReactNode; error?: string; children: ReactNode }) {
  const id = useId();
  const noteId = `${id}-note`;
  const note = props.error || props.hint;
  const control = isValidElement(props.children)
    ? cloneElement(props.children as ReactElement<Record<string, unknown>>, {
        "aria-describedby": note ? noteId : undefined,
        "aria-invalid": props.error ? true : undefined,
      })
    : props.children;
  return (
    <div className={`field${props.error ? " has-error" : ""}`}>
      <label>
        <span className="field-label">{props.label}</span>
        {control}
      </label>
      {props.error ? (
        <span id={noteId} className="field-error">{props.error}</span>
      ) : props.hint ? (
        <span id={noteId} className="field-hint">{props.hint}</span>
      ) : null}
    </div>
  );
}

export function Notice(props: { kind: "info" | "ok" | "warn" | "error"; children: ReactNode }) {
  return <div className={`notice notice-${props.kind}`} role={props.kind === "error" ? "alert" : undefined}>{props.children}</div>;
}
