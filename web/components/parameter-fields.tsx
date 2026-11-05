"use client";
import { Plus, Trash2 } from "lucide-react";
import type { ParameterDefinition } from "@/lib/api";
import { Field } from "./ui";
export function parameterDefaults(definitions: ParameterDefinition[]) {
  return Object.fromEntries(
    definitions
      .filter((item) => item.default !== undefined)
      .map((item) => [item.name, item.default]),
  );
}
export function parseParameter(value: string, type: string): unknown {
  if (type === "integer") {
    const number = Number(value);
    if (!Number.isInteger(number)) throw new Error("Enter a whole number.");
    return number;
  }
  if (type === "number") {
    const number = Number(value);
    if (!Number.isFinite(number)) throw new Error("Enter a valid number.");
    return number;
  }
  if (type === "boolean") {
    if (value !== "true" && value !== "false")
      throw new Error("Enter true or false.");
    return value === "true";
  }
  if (type === "json") return JSON.parse(value);
  return value;
}
export function ParameterValues({
  definitions,
  values,
  onChange,
}: {
  definitions: ParameterDefinition[];
  values: Record<string, unknown>;
  onChange: (value: Record<string, unknown>) => void;
}) {
  return (
    <div className="form-grid">
      {definitions.map((item) => (
        <Field
          key={item.name}
          label={item.name + (item.required ? " *" : "")}
          hint={`${item.type}${item.default !== undefined ? " · default: " + JSON.stringify(item.default) : ""}`}
        >
          <input
            type={item.type === "date" ? "date" : "text"}
            value={
              typeof values[item.name] === "string"
                ? String(values[item.name])
                : values[item.name] === undefined
                  ? ""
                  : JSON.stringify(values[item.name])
            }
            placeholder={
              item.type === "boolean"
                ? "true or false"
                : item.type === "json"
                  ? '{"key":"value"}'
                  : "Use default"
            }
            list={item.choices ? `parameter-${item.name}` : undefined}
            onChange={(event) => {
              if (event.target.value === "") {
                const next = { ...values };
                delete next[item.name];
                onChange(next);
                return;
              }
              let value: unknown = event.target.value;
              try {
                value = parseParameter(event.target.value, item.type);
              } catch {}
              onChange({ ...values, [item.name]: value });
            }}
          />
          {item.choices ? (
            <datalist id={`parameter-${item.name}`}>
              {item.choices.map((choice, index) => (
                <option
                  key={index}
                  value={
                    typeof choice === "string" ? choice : JSON.stringify(choice)
                  }
                />
              ))}
            </datalist>
          ) : null}
        </Field>
      ))}
    </div>
  );
}
export function ParameterDefinitions({
  value,
  onChange,
}: {
  value: ParameterDefinition[];
  onChange: (value: ParameterDefinition[]) => void;
}) {
  function update(index: number, patch: Partial<ParameterDefinition>) {
    onChange(
      value.map((item, i) => (i === index ? { ...item, ...patch } : item)),
    );
  }
  return (
    <div className="parameter-definitions">
      {value.map((item, index) => (
        <div className="parameter-row" key={index}>
          <Field label="Name">
            <input
              required
              pattern="[A-Za-z_][A-Za-z0-9_]*"
              value={item.name}
              onChange={(event) => update(index, { name: event.target.value })}
            />
          </Field>
          <Field label="Type">
            <select
              value={item.type}
              onChange={(event) =>
                update(index, {
                  type: event.target.value as ParameterDefinition["type"],
                  default: undefined,
                })
              }
            >
              {["string", "integer", "number", "boolean", "date", "json"].map(
                (type) => (
                  <option key={type}>{type}</option>
                ),
              )}
            </select>
          </Field>
          <Field label="Default">
            <input
              value={
                item.default === undefined
                  ? ""
                  : typeof item.default === "string"
                    ? item.default
                    : JSON.stringify(item.default)
              }
              onChange={(event) => {
                try {
                  update(index, {
                    default:
                      event.target.value === ""
                        ? undefined
                        : parseParameter(event.target.value, item.type),
                  });
                } catch {
                  update(index, { default: event.target.value });
                }
              }}
            />
          </Field>
          <label className="checkbox-field">
            <input
              type="checkbox"
              checked={!!item.required}
              onChange={(event) =>
                update(index, { required: event.target.checked })
              }
            />
            Required
          </label>
          <button
            type="button"
            className="icon-button"
            aria-label={`Remove parameter ${item.name}`}
            onClick={() => onChange(value.filter((_, i) => i !== index))}
          >
            <Trash2 size={14} />
          </button>
        </div>
      ))}
      <button
        type="button"
        className="button compact"
        onClick={() =>
          onChange([
            ...value,
            { name: `parameter_${value.length + 1}`, type: "string" },
          ])
        }
      >
        <Plus size={13} />
        Add parameter
      </button>
      <p className="form-note">
        Notebook code reads values from <code>params["name"]</code>. Manual and
        scheduled runs can override defaults.
      </p>
    </div>
  );
}
