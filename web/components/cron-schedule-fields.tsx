"use client";

import { useId, useState } from "react";
import { Field } from "./ui";

type BasicSchedule = {
  frequency: "hourly" | "daily" | "weekly";
  interval: number;
  minute: number;
  time: string;
  days: number[];
};

const weekdays = [
  { value: 1, label: "Mon", name: "Monday" },
  { value: 2, label: "Tue", name: "Tuesday" },
  { value: 3, label: "Wed", name: "Wednesday" },
  { value: 4, label: "Thu", name: "Thursday" },
  { value: 5, label: "Fri", name: "Friday" },
  { value: 6, label: "Sat", name: "Saturday" },
  { value: 0, label: "Sun", name: "Sunday" },
];
const defaultBasic: BasicSchedule = {
  frequency: "daily",
  interval: 1,
  minute: 0,
  time: "02:00",
  days: [1, 2, 3, 4, 5],
};
const pad = (value: number) => String(value).padStart(2, "0");

function integer(value: string, min: number, max: number): number | null {
  if (!/^\d+$/.test(value)) return null;
  const parsed = Number(value);
  return parsed >= min && parsed <= max ? parsed : null;
}

function weekday(value: string): number | null {
  const number = integer(value, 0, 7);
  if (number !== null) return number;
  const index = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"].indexOf(
    value.toLowerCase(),
  );
  return index === -1 ? null : index;
}

/** Parse only schedules that the Basic controls can represent exactly. */
export function parseBasicCron(cron: string): BasicSchedule | null {
  const fields = cron.trim().split(/\s+/);
  if (fields.length !== 5) return null;
  const [minuteField, hourField, day, month, dayOfWeek] = fields;
  if (day !== "*" || month !== "*") return null;
  const minute = integer(minuteField, 0, 59);
  if (minute === null) return null;
  if (dayOfWeek === "*" && (hourField === "*" || /^\*\/\d+$/.test(hourField))) {
    const interval = hourField === "*" ? 1 : integer(hourField.slice(2), 1, 23);
    return interval === null
      ? null
      : { ...defaultBasic, frequency: "hourly", interval, minute };
  }
  const hour = integer(hourField, 0, 23);
  if (hour === null) return null;
  const time = `${pad(hour)}:${pad(minute)}`;
  if (dayOfWeek === "*") return { ...defaultBasic, time, minute };
  const days = new Set<number>();
  for (const part of dayOfWeek.split(",")) {
    const ends = part.split("-");
    if (ends.length > 2) return null;
    const start = weekday(ends[0]);
    const end = ends.length === 1 ? start : weekday(ends[1]);
    if (start === null || end === null || start > end) return null;
    for (let value = start; value <= end; value++) days.add(value % 7);
  }
  return days.size
    ? {
        ...defaultBasic,
        frequency: "weekly",
        time,
        minute,
        days: [...days].sort((a, b) => a - b),
      }
    : null;
}

export function basicCron(value: BasicSchedule): string {
  if (value.frequency === "hourly")
    return `${value.minute} */${value.interval} * * *`;
  const match = /^(\d{2}):(\d{2})$/.exec(value.time);
  if (!match) return "";
  return `${Number(match[2])} ${Number(match[1])} * * ${value.frequency === "weekly" ? [...value.days].sort((a, b) => a - b).join(",") : "*"}`;
}

export default function CronScheduleFields({
  cron,
  timezone,
  onCronChange,
  onTimezoneChange,
  disabled = false,
}: {
  cron: string;
  timezone: string;
  onCronChange: (value: string) => void;
  onTimezoneChange: (value: string) => void;
  disabled?: boolean;
}) {
  const id = useId();
  const [mode, setMode] = useState<"basic" | "advanced">(() =>
    parseBasicCron(cron) ? "basic" : "advanced",
  );
  const [basic, setBasic] = useState<BasicSchedule>(
    () => parseBasicCron(cron) || defaultBasic,
  );

  function updateBasic(changes: Partial<BasicSchedule>) {
    const next = { ...basic, ...changes };
    setBasic(next);
    onCronChange(basicCron(next));
  }

  return (
    <div style={{ gridColumn: "1 / -1", minWidth: 0 }}>
      <h3>Schedule</h3>
      <div className="schedule-tabs" role="group" aria-label="Schedule options">
        <button
          type="button"
          className={mode === "basic" ? "active" : ""}
          aria-pressed={mode === "basic"}
          disabled={disabled}
          onClick={() => {
            setBasic(parseBasicCron(cron) || basic);
            setMode("basic");
          }}
        >
          Basic
        </button>
        <button
          type="button"
          className={mode === "advanced" ? "active" : ""}
          aria-pressed={mode === "advanced"}
          disabled={disabled}
          onClick={() => setMode("advanced")}
        >
          Advanced
        </button>
      </div>
      <div className="schedule-panel">
        {mode === "basic" ? (
          <>
            <div className="form-grid">
              <Field label="Repeat">
                <select
                  value={basic.frequency}
                  disabled={disabled}
                  onChange={(event) =>
                    updateBasic({
                      frequency: event.target
                        .value as BasicSchedule["frequency"],
                    })
                  }
                >
                  <option value="daily">Every day</option>
                  <option value="weekly">On selected weekdays</option>
                  <option value="hourly">Every few hours</option>
                </select>
              </Field>
              {basic.frequency === "hourly" ? (
                <Field label="Every">
                  <select
                    value={basic.interval}
                    disabled={disabled}
                    onChange={(event) =>
                      updateBasic({ interval: Number(event.target.value) })
                    }
                  >
                    {Array.from({ length: 23 }, (_, index) => index + 1).map(
                      (value) => (
                        <option key={value} value={value}>
                          {value} {value === 1 ? "hour" : "hours"}
                        </option>
                      ),
                    )}
                  </select>
                </Field>
              ) : (
                <Field label="Run at">
                  <input
                    type="time"
                    required
                    disabled={disabled}
                    value={basic.time}
                    onChange={(event) =>
                      updateBasic({ time: event.target.value })
                    }
                  />
                </Field>
              )}
            </div>
            {basic.frequency === "hourly" ? (
              <Field label="Minute past the hour">
                <select
                  value={basic.minute}
                  disabled={disabled}
                  onChange={(event) =>
                    updateBasic({ minute: Number(event.target.value) })
                  }
                >
                  {Array.from({ length: 60 }, (_, value) => (
                    <option key={value} value={value}>
                      {pad(value)}
                    </option>
                  ))}
                </select>
              </Field>
            ) : null}
            {basic.frequency === "weekly" ? (
              <fieldset
                className="weekday-fieldset"
                disabled={disabled}
                aria-describedby={`${id}-days-hint`}
              >
                <legend>Days of the week</legend>
                <div className="weekday-buttons">
                  {weekdays.map((day) => {
                    const selected = basic.days.includes(day.value);
                    return (
                      <label
                        key={day.value}
                        className={selected ? "selected" : ""}
                      >
                        <input
                          type="checkbox"
                          aria-label={day.name}
                          checked={selected}
                          disabled={
                            disabled || (selected && basic.days.length === 1)
                          }
                          onChange={() =>
                            updateBasic({
                              days: selected
                                ? basic.days.filter(
                                    (value) => value !== day.value,
                                  )
                                : [...basic.days, day.value],
                            })
                          }
                        />
                        <span>{day.label}</span>
                      </label>
                    );
                  })}
                </div>
                <small className="muted" id={`${id}-days-hint`}>
                  Keep at least one day selected.
                </small>
              </fieldset>
            ) : null}
            {parseBasicCron(cron) === null ? (
              <p className="info-banner">
                Your custom expression remains active until you change a Basic
                option.
              </p>
            ) : null}
          </>
        ) : (
          <Field
            label="Cron expression"
            hint="Five fields: minute, hour, day of month, month, weekday. Sunday is 0. Example: 0 2 * * * runs daily at 02:00."
          >
            <textarea
              rows={2}
              required
              maxLength={100}
              spellCheck={false}
              autoCapitalize="off"
              disabled={disabled}
              value={cron}
              onChange={(event) => onCronChange(event.target.value)}
            />
          </Field>
        )}
        <Field
          label="Time zone"
          hint="Schedule times use this time zone, including its daylight-saving rules."
        >
          <input
            required
            maxLength={100}
            placeholder="UTC or America/New_York"
            list={`${id}-zones`}
            value={timezone}
            disabled={disabled}
            onChange={(event) => onTimezoneChange(event.target.value)}
          />
          <datalist id={`${id}-zones`}>
            {[
              "UTC",
              "America/New_York",
              "America/Chicago",
              "America/Denver",
              "America/Los_Angeles",
              "Europe/London",
              "Europe/Paris",
              "Asia/Tokyo",
              "Australia/Sydney",
            ].map((zone) => (
              <option key={zone} value={zone} />
            ))}
          </datalist>
        </Field>
        <p className="muted" style={{ overflowWrap: "anywhere" }}>
          Current schedule: <code>{cron || "Enter a schedule"}</code> ·{" "}
          {timezone || "Choose a time zone"}
        </p>
      </div>
    </div>
  );
}
